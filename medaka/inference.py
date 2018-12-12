from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import functools
import inspect
import itertools
import logging
from math import ceil
import os
import queue
import threading
import time
from timeit import default_timer as now

import numpy as np
import pysam

from medaka import vcf
from medaka.datastore import DataStore, DataIndex
from medaka.common import (get_regions, decoding, grouper, mkdir_p, Sample,
                           _gap_, threadsafe_generator, get_named_logger)
from medaka.features import SampleGenerator


def weighted_categorical_crossentropy(weights):
    """
    A weighted version of keras.objectives.categorical_crossentropy
    @url: https://gist.github.com/wassname/ce364fddfc8a025bfab4348cf5de852d
    @author: wassname

    Variables:
        weights: numpy array of shape (C,) where C is the number of classes

    Usage:
        weights = np.array([0.5,2,10]) # Class one at 0.5, class 2 twice the normal weights, class 3 10x.
        loss = weighted_categorical_crossentropy(weights)
        model.compile(loss=loss,optimizer='adam')
    """

    from keras import backend as K
    weights = K.variable(weights)

    def loss(y_true, y_pred):
        # scale predictions so that the class probas of each sample sum to 1
        y_pred /= K.sum(y_pred, axis=-1, keepdims=True)
        # clip to prevent NaN's and Inf's
        y_pred = K.clip(y_pred, K.epsilon(), 1 - K.epsilon())
        # calc
        loss = y_true * K.log(y_pred) * weights
        loss = -K.sum(loss, -1)
        return loss

    return loss


def build_model(chunk_size, feature_len, num_classes, gru_size=128, input_dropout=0.0,
                inter_layer_dropout=0.0, recurrent_dropout=0.0):
    """Builds a bidirectional GRU model
    :param chunk_size: int, number of pileup columns in a sample.
    :param feature_len: int, number of features for each pileup column.
    :param num_classes: int, number of output class labels.
    :param gru_size: int, size of each GRU layer.
    :param input_dropout: float, fraction of the input feature-units to drop.
    :param inter_layer_dropout: float, fraction of units to drop between layers.
    :param recurrent_dropout: float, fraction of units to drop within the recurrent state.
    :returns: `keras.models.Sequential` object.
    """

    from keras.models import Sequential
    from keras.layers import Dense, GRU, Dropout
    from keras.layers.wrappers import Bidirectional

    model = Sequential()

    gru1 = GRU(gru_size, activation='tanh', return_sequences=True, name='gru1',
               dropout=input_dropout, recurrent_dropout=recurrent_dropout)
    gru2 = GRU(gru_size, activation='tanh', return_sequences=True, name='gru2',
               dropout=inter_layer_dropout, recurrent_dropout=recurrent_dropout)

    # Bidirectional wrapper takes a copy of the first argument and reverses
    #   the direction. Weights are independent between components.
    model.add(Bidirectional(gru1, input_shape=(chunk_size, feature_len)))

    model.add(Bidirectional(gru2, input_shape=(chunk_size, feature_len)))

    if inter_layer_dropout > 0:
        model.add(Dropout(inter_layer_dropout))

    # see keras #10417 for why we specify input shape
    model.add(Dense(
        num_classes, activation='softmax', name='classify',
        input_shape=(chunk_size, 2 * feature_len)
    ))

    return model


def qscore(y_true, y_pred):
    from keras import backend as K
    error = K.cast(K.not_equal(
        K.max(y_true, axis=-1), K.cast(K.argmax(y_pred, axis=-1), K.floatx())),
        K.floatx()
    )
    error = K.sum(error) / K.sum(K.ones_like(error))
    return -10.0 * 0.434294481 * K.log(error)


def run_training(train_name, batcher, model_fp=None,
                 epochs=5000, class_weight=None, n_mini_epochs=1):
    """Run training."""
    from keras.callbacks import ModelCheckpoint, CSVLogger, TensorBoard, EarlyStopping

    logger = get_named_logger('RunTraining')

    if model_fp is None:
        model_kwargs = { k:v.default for (k,v) in inspect.signature(build_model).parameters.items()
                         if v.default is not inspect.Parameter.empty}
    else:
        model_kwargs = load_yaml_data(model_fp, _model_opt_path_)
        assert model_kwargs is not None

    opt_str = '\n'.join(['{}: {}'.format(k,v) for k, v in model_kwargs.items()])
    logger.info('Building model with: \n{}'.format(opt_str))
    num_classes = len(batcher.label_counts)
    timesteps, feat_dim = batcher.feature_shape
    model = build_model(timesteps, feat_dim, num_classes, **model_kwargs)

    if model_fp is not None and os.path.splitext(model_fp)[-1] != '.yml':
        logger.info("Loading weights from {}".format(model_fp))
        model.load_weights(model_fp)

    msg = "feat_dim: {}, timesteps: {}, num_classes: {}"
    logger.info(msg.format(feat_dim, timesteps, num_classes))
    model.summary()

    model_details = batcher.meta.copy()
    model_details['medaka_model_kwargs'] = model_kwargs
    model_details['medaka_label_decoding'] = batcher.label_decoding

    opts = dict(verbose=1, save_best_only=True, mode='max')

    # define class here to avoid top-level keras import
    class ModelMetaCheckpoint(ModelCheckpoint):
        """Custom ModelCheckpoint to add medaka-specific metadata to model files"""
        def __init__(self, medaka_meta, *args, **kwargs):
            super(ModelMetaCheckpoint, self).__init__(*args, **kwargs)
            self.medaka_meta = medaka_meta

        def on_epoch_end(self, epoch, logs=None):
            super(ModelMetaCheckpoint, self).on_epoch_end(epoch, logs)
            filepath = self.filepath.format(epoch=epoch + 1, **logs)
            with DataStore(filepath, 'a') as ds:
                ds.meta.update(self.medaka_meta)

    callbacks = [
        # Best model according to training set accuracy
        ModelMetaCheckpoint(model_details, os.path.join(train_name, 'model.best.hdf5'),
                            monitor='acc', **opts),
        # Best model according to validation set accuracy
        ModelMetaCheckpoint(model_details, os.path.join(train_name, 'model.best.val.hdf5'),
                        monitor='val_acc', **opts),
        # Best model according to validation set qscore
        ModelMetaCheckpoint(model_details, os.path.join(train_name, 'model.best.val.qscore.hdf5'),
                        monitor='val_qscore', **opts),
        # Checkpoints when training set accuracy improves
        ModelMetaCheckpoint(model_details, os.path.join(train_name, 'model-acc-improvement-{epoch:02d}-{acc:.2f}.hdf5'),
                        monitor='acc', **opts),
        ModelMetaCheckpoint(model_details, os.path.join(train_name, 'model-val_acc-improvement-{epoch:02d}-{val_acc:.2f}.hdf5'),
                        monitor='val_acc', **opts),
        # Stop when no improvement, patience is number of epochs to allow no improvement
        EarlyStopping(monitor='val_loss', patience=20),
        # Log of epoch stats
        CSVLogger(os.path.join(train_name, 'training.log')),
        # Allow us to run tensorboard to see how things are going. Some
        #   features require validation data, not clear why.
        #TensorBoard(log_dir=os.path.join(train_name, 'logs'),
        #            histogram_freq=5, batch_size=100, write_graph=True,
        #            write_grads=True, write_images=True)
    ]

    if class_weight is not None:
        loss = weighted_categorical_crossentropy(class_weight)
        logger.info("Using weighted_categorical_crossentropy loss function")
    else:
        loss = 'sparse_categorical_crossentropy'
        logger.info("Using {} loss function".format(loss))

    model.compile(
       loss=loss,
       optimizer='rmsprop',
       metrics=['accuracy', qscore],
    )

    if n_mini_epochs == 1:
        logging.info("Not using mini_epochs, an epoch is a full traversal of the training data")
    else:
        logging.info("Using mini_epochs, an epoch is a traversal of 1/{} of the training data".format(n_mini_epochs))

    # fit generator
    model.fit_generator(
        batcher.gen_train(), steps_per_epoch=ceil(batcher.n_train_batches/n_mini_epochs),
        validation_data=batcher.gen_valid(), validation_steps=batcher.n_valid_batches,
        max_queue_size=8, workers=8, use_multiprocessing=False,
        epochs=epochs,
        callbacks=callbacks,
        class_weight=class_weight,
    )

    # stop batching threads
    batcher.stop()
    # TODO this is hanging - why?


class TrainBatcher():
    def __init__(self, features, max_label_len, validation=0.2, seed=0, sparse_labels=True, batch_size=500):
        """
        Class to server up batches of training / validation data.

        :param features: iterable of str, training feature files.
        :param max_label_len: int, maximum label length, longer labels will be truncated.
        :param validation: float, fraction of batches to use for validation, or
                iterable of str, validation feature files.
        :param seed: int, random seed for separation of batches into training/validation.
        :param sparse_labels: bool, create sparse labels.
        """
        self.logger = get_named_logger('TrainBatcher')

        self.features = features
        self.max_label_len = max_label_len
        self.validation = validation
        self.seed = seed
        self.sparse_labels = sparse_labels
        self.batch_size = batch_size

        di = DataIndex(self.features)
        self.samples = di.samples.copy()
        self.meta = di.meta.copy()
        self.label_counts = self.meta['medaka_label_counts']

        # check sample size using first batch
        test_sample, test_fname = self.samples[0]
        with DataStore(test_fname) as ds:
            self.feature_shape = ds.load_sample(test_sample).features.shape
        self.logger.info("Sample features have shape {}".format(self.feature_shape))

        if isinstance(self.validation, float):
            np.random.seed(self.seed)
            np.random.shuffle(self.samples)
            n_sample_train = int((1 - self.validation) * len(self.samples))
            self.train_samples = self.samples[:n_sample_train]
            self.valid_samples = self.samples[n_sample_train:]
            msg = 'Randomly selected {} ({:3.2%}) of features for validation (seed {})'
            self.logger.info(msg.format(len(self.valid_samples), self.validation, self.seed))
        else:
            self.train_samples = self.samples
            self.valid_samples = DataIndex(self.validation).samples.copy()
            msg = 'Found {} validation samples equivalent to {:3.2%} of all the data'
            fraction = len(self.valid_samples) / len(self.valid_samples) + len(self.train_samples)
            self.logger.info(msg.format(len(self.valid_samples), fraction))

        self.n_train_batches = ceil(len(self.train_samples) / batch_size)
        self.n_valid_batches = ceil(len(self.valid_samples) / batch_size)

        msg = 'Got {} samples in {} batches ({} labels) for {}'
        self.logger.info(msg.format(len(self.train_samples),
                                    self.n_train_batches,
                                    len(self.train_samples) * self.feature_shape[0],
                                    'training'))
        self.logger.info(msg.format(len(self.valid_samples),
                                    self.n_valid_batches,
                                    len(self.valid_samples) * self.feature_shape[0],
                                    'validation'))

        self.n_classes = len(self.label_counts)

        # get label encoding, given max_label_len
        self.logger.info("Max label length: {}".format(self.max_label_len if self.max_label_len is not None else 'inf'))
        self.label_encoding, self.label_decoding, self.label_counts = process_labels(self.label_counts, max_label_len=self.max_label_len)

        prep_func = functools.partial(TrainBatcher.sample_to_x_y,
                                      max_label_len=self.max_label_len,
                                      label_encoding=self.label_encoding,
                                      sparse_labels=self.sparse_labels,
                                      n_classes=self.n_classes)

        self._valid_queue = BatchQueue(self.valid_samples, prep_func,
                                       self.batch_size, self.seed,
                                       name='ValidBatcher',
                                       maxsize=min(2 * self.n_valid_batches, 100))
        self._train_queue = BatchQueue(self.train_samples, prep_func,
                                       self.batch_size, self.seed,
                                       name='TrainBatcher',
                                       maxsize=min(2 * self.n_train_batches, 100))


    @staticmethod
    def sample_to_x_y(sample, max_label_len, label_encoding, sparse_labels, n_classes):
        """Convert a `Sample` object into an x,y tuple for training.

        :param sample: (filename, sample key)
        :param max_label_len: int, maximum label length, longer labels will be truncated.
        :param label_encoding: {label: int encoded label}.
        :param sparse_labels: bool, create sparse labels.
        :param n_classes: int, number of label classes.
        :returns: (np.ndarray of inputs, np.ndarray of labels)
        """
        sample_key, sample_file = sample

        with DataStore(sample_file) as ds:
            s = ds.load_sample(sample_key)
        if s.labels is None:
            raise ValueError("Cannot train without labels.")
        x = s.features
        # labels can either be unicode strings or (base, length) integer tuples
        if isinstance(s.labels[0], np.unicode):
            # TODO: is this ever used now we have dispensed with tview code?
            y = np.fromiter((label_encoding[l[:min(max_label_len, len(l))]]
                               for l in s.labels), dtype=int, count=len(s.labels))
        else:
            y = np.fromiter((label_encoding[tuple((l['base'], min(max_label_len, l['run_length'])))]
                             for l in s.labels), dtype=int, count=len(s.labels))
        y = y.reshape(y.shape + (1,))
        if not sparse_labels:
            from keras.utils.np_utils import to_categorical
            y = to_categorical(y, num_classes=n_classes)
        return x, y


    def stop(self):
        self._train_queue.stop()
        self._valid_queue.stop()


    @threadsafe_generator
    def gen_train(self):
        yield from self._train_queue.yield_batches()

    @threadsafe_generator
    def gen_valid(self):
        yield from self._valid_queue.yield_batches()


class BatchQueue(object):
    def  __init__(self, samples, prep_func, batch_size, seed=None, name='Batcher', maxsize=100):
        """Load and queue training samples into batches from `.hdf` files.

        :param samples: tuples of (filename, hdf sample key).
        :param prep_func: function to transform a sample to x,y data.
        :param batch_size: group samples by this number.
        :param seed: seed for shuffling.
        :param name: str, name for logger.
        :param maxsize: int, maximum queue size.

        Once initialized batches can be retrieved using batch_q._queue.get().

        """
        self.samples = samples
        self.prep_func = prep_func
        self.batch_size = batch_size

        if seed is not None:
            np.random.seed(seed)

        self.logger = get_named_logger(name)
        self._queue = queue.Queue(maxsize=maxsize)
        self.stopped = threading.Event()
        self.qthread = threading.Thread(target=self._fill_queue)
        self.qthread.start()
        time.sleep(2)
        self.logger.info("Started reading samples from files with queue size {}".format(maxsize))


    def stop(self):
        self.logger.info("About to stop.")
        self.stopped.set()
        self.logger.info("Waiting for read thread.")
        self.qthread.join(2)
        if self.qthread.is_alive:
            self.logger.critical("Read thread did not terminate.")


    def _fill_queue(self):
        with ProcessPoolExecutor(1) as executor:
            epoch = 0
            while not self.stopped.is_set():
                batch = 0
                np.random.shuffle(self.samples)
                for samples in grouper(iter(self.samples), batch_size=self.batch_size):
                    items = []
                    t0 = now()
                    for sample in samples:
                        res = executor.submit(self.prep_func, sample)
                        items.append(res.result())
                    xs, ys = zip(*items)
                    res = np.stack(xs), np.stack(ys)
                    t1 = now()
                    self._queue.put(res)
                    self.logger.debug("Took {:5.3}s to load batch {} (epoch {})".format(t1-t0, batch, epoch))
                    batch += 1
                epoch += 1
            self.logger.info("Ended batching.")


    @threadsafe_generator
    def yield_batches(self):
        try:
            while True:
                yield self._queue.get()
        except Exception as e:
            self.logger.critical("Exception caught why yielding batches: {}".format(e))
            self.stop()
            raise e


class VarQueue(list):

    @property
    def last_pos(self):
        if len(self) == 0:
            return None
        else:
            return self[-1].pos

    def write(self, vcf_fh):
        if len(self) > 1:
            are_dels = all(len(x.ref) == 2 for x in self)
            are_same_ref = len(set(x.chrom for x in self)) == 1
            if are_dels and are_same_ref:
                name = self[0].chrom
                pos = self[0].pos
                ref = ''.join((x.ref[0] for x in self))
                ref += self[-1].ref[-1]
                alt = ref[0]

                merged_var = vcf.Variant(name, pos, ref, alt, info=info)
                vcf_fh.write_variant(merged_var)
            else:
                raise ValueError('Cannot merge variants: {}.'.format(self))
        elif len(self) == 1:
            vcf_fh.write_variant(self[0])
        del self[:]


class VCFChunkWriter(object):
    def __init__(self, fname, chrom, start, end, reference_fasta, label_decoding):
        vcf_region_str = '{}:{}-{}'.format(chrom, start, end) #is this correct?
        self.label_decoding = label_decoding
        self.logger = get_named_logger('VCFWriter')
        self.logger.info("Writing variants for {}".format(vcf_region_str))

        vcf_meta = ['region={}'.format(vcf_region_str)]
        self.writer = vcf.VCFWriter(fname, meta_info=vcf_meta)
        self.ref_fasta = pysam.FastaFile(reference_fasta)

    def __enter__(self):
        self.writer.__enter__()
        self.ref_fasta.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.writer.__exit__(exc_type, exc_val, exc_tb)
        self.ref_fasta.__exit__(exc_type, exc_val, exc_tb)

    def add_chunk(self, sample, pred):
        # Write consensus alts to vcf
        cursor = 0
        var_queue = list()
        ref_seq = self.ref_fasta.fetch(sample.ref_name)
        for pos, grp in itertools.groupby(sample.positions['major']):
            end = cursor + len(list(grp))
            alt = ''.join(self.label_decoding[x] for x in pred[cursor:end]).replace(_gap_, '')
            # For simple insertions and deletions in which either
            #   the REF or one of the ALT alleles would otherwise be
            #   null/empty, the REF and ALT Strings must include the
            #   base before the event (which must be reflected in
            #   the POS field), unless the event occurs at position
            #   1 on the contig in which case it must include the
            #   base after the event
            if alt == '':
                # deletion
                if pos == 0:
                    # the "unless case"
                    ref = ref_seq[1]
                    alt = ref_seq[1]
                else:
                    # the usual case
                    pos = pos - 1
                    ref = ref_seq[pos:pos+2]
                    alt = ref_seq[pos]
            else:
                ref = ref_seq[pos]

            # Merging of variants produced by considering major.{minor} positions
            # These are of the form:
            #    X -> Y          - subs
            #    prev.X -> prev  - deletion
            #    X -> Xyy..      - insertion
            # In the second case we may need to merge variants from consecutive
            # major positions.
            if alt == ref:
                self.write(var_queue)
                var_queue = list()
            else:
                var = vcf.Variant(sample.ref_name, pos, ref, alt)
                if len(var_queue) == 0 or pos - var_queue[-1].pos == 1:
                    var_queue.append(var)
                else:
                    self.write(var_queue)
                    var_queue = [var]
            cursor = end
        self.write(var_queue)


    def write(self, var_queue):
        if len(var_queue) > 1:
            are_dels = all(len(x.ref) == 2 for x in var_queue)
            are_same_ref = len(set(x.chrom for x in var_queue)) == 1
            if are_dels and are_same_ref:
                name = var_queue[0].chrom
                pos = var_queue[0].pos
                ref = ''.join((x.ref[0] for x in var_queue))
                ref += var_queue[-1].ref[-1]
                alt = ref[0]

                merged_var = vcf.Variant(name, pos, ref, alt)
                self.writer.write_variant(merged_var)
            else:
                raise ValueError('Cannot merge variants: {}.'.format(var_queue))
        elif len(var_queue) == 1:
            self.writer.write_variant(var_queue[0])


def run_prediction(sample_gen, output, batch_size=200, threads=1):
    """Inference worker."""
    from keras.models import load_model
    from keras import backend as K

    logger = get_named_logger('PWorker')

    logger.info("Setting tensorflow threads to {}.".format(threads))
    K.set_session(K.tf.Session(
        config=K.tf.ConfigProto(
            intra_op_parallelism_threads=threads,
            inter_op_parallelism_threads=threads)
    ))

    model = load_model(sample_gen.model, custom_objects={'qscore': qscore})
    time_steps = model.get_input_shape_at(0)[1]
    if time_steps != sample_gen.chunk_len:
        logger.info("Rebuilding model according to chunk_size: {}->{}".format(time_steps, sample_gen.chunk_len))
        feat_dim = model.get_input_shape_at(0)[2]
        num_classes = model.get_output_shape_at(-1)[-1]
        model = build_model(sample_gen.chunk_len, feat_dim, num_classes)

    logger.info("Loading weights from {}".format(sample_gen.model))
    model.load_weights(sample_gen.model)

    if logger.level == logging.DEBUG:
        model.summary()

    logger.info("Initialising pileup.")
    n_samples = sample_gen.n_samples
    logger.info("Running inference for {} chunks.".format(n_samples))
    batches = grouper(sample_gen.samples, batch_size)

    with DataStore(output, 'a') as ds:
        n_samples_done = 0

        t0 = now()
        tlast = t0
        for data in batches:
            x_data = np.stack((x.features for x in data))
            class_probs = model.predict(x_data, batch_size=batch_size, verbose=0)

            n_samples_done += x_data.shape[0]
            t1 = now()
            if t1 - tlast > 10:
                tlast = t1
                msg = '{:.1%} Done ({}/{} samples) in {:.1f}s'
                logger.info(msg.format(n_samples_done / n_samples, n_samples_done, n_samples, t1 - t0))

            best = np.argmax(class_probs, -1)
            for sample, prob, pred in zip(data, class_probs, best):
                # write out positions and predictions for later analysis
                sample_d = sample._asdict()
                sample_d['label_probs'] = prob
                sample_d['features'] = None  # to keep file sizes down
                ds.write_sample(Sample(**sample_d))

    logger.info('All done')
    return sample_gen.region


def predict(args):
    """Inference program."""
    args.regions = get_regions(args.bam, region_strs=args.regions)
    logger = get_named_logger('Predict')
    logger.info('Processing region(s): {}'.format(' '.join(str(r) for r in args.regions)))

    # write class names to output
    with DataStore(args.model) as ds:
        meta = ds.meta
    with DataStore(args.output, 'w') as ds:
        ds.update_meta(meta)

    for region in args.regions:
        chunk_len, chunk_ovlp = args.chunk_len, args.chunk_ovlp
        if region.size < args.chunk_len:
            chunk_len = region.size // 2
            chunk_ovlp = chunk_len // 10 # still need overlap as features will be longer
        data_gen = SampleGenerator(
            args.bam, region, args.model, args.rle_ref, args.read_fraction,
            chunk_len=chunk_len, chunk_overlap=chunk_ovlp)
        run_prediction(
            data_gen, args.output, batch_size=args.batch_size, threads=args.threads
        )


def process_labels(label_counts, max_label_len=10):
    """Create map from full labels to (encoded) truncated labels.

    :param label_counrs: `Counter` obj of label counts.
    :param max_label_len: int, maximum label length, longer labels will be truncated.
    :returns:
    :param label_encoding: {label: int encoded label}.
    :param sparse_labels: bool, create sparse labels.
    :param n_classes: int, number of label classes.
    :returns: ({label: int encoding}, [label decodings], `Counter` of truncated counts).
    """
    logger = get_named_logger('Labelling')

    old_labels = [k for k in label_counts.keys()]
    if type(old_labels[0]) == tuple:
        new_labels = (l[1] * decoding[l[0]].upper() for l in old_labels)
    else:
        new_labels = [l for l in old_labels]

    if max_label_len < np.inf:
        new_labels = [l[:max_label_len] for l in new_labels]

    old_to_new = dict(zip(old_labels, new_labels))
    label_decoding = list(sorted(set(new_labels)))
    label_encoding = { l: label_decoding.index(old_to_new[l]) for l in old_labels}
    logger.info("Label encoding dict is:\n{}".format('\n'.join(
        '{}: {}'.format(k, v) for k, v in label_encoding.items()
    )))

    new_counts = Counter()
    for l in old_labels:
        new_counts[label_encoding[l]] += label_counts[l]
    logger.info("New label counts {}".format(new_counts))

    return label_encoding, label_decoding, new_counts


def train(args):
    """Training program."""
    train_name = args.train_name
    mkdir_p(train_name, info='Results will be overwritten.')

    logger = get_named_logger('Training')
    logger.debug("Loading datasets:\n{}".format('\n'.join(args.features)))

    sparse_labels = not args.balanced_weights

    args.validation = args.validation_features if args.validation_features is not None else args.validation_split

    batcher = TrainBatcher(args.features, args.max_label_len, args.validation,
                           args.seed, sparse_labels, args.batch_size)

    if args.balanced_weights:
        n_labels = sum(batcher.label_counts.values())
        n_classes = len(batcher.label_counts)
        class_weight = {k: float(n_labels)/(n_classes * count) for (k, count) in batcher.label_counts.items()}
        class_weight = np.array([class_weight[c] for c in sorted(class_weight.keys())])
    else:
        class_weight = None

    h = lambda d, i: d[i] if d is not None else 1
    logger.info("Label statistics are:\n{}".format('\n'.join(
        '{} ({}) {} (w. {:9.6f})'.format(i, l, batcher.label_counts[i], h(class_weight, i))
            for i, l in enumerate(batcher.label_decoding)
    )))

    run_training(train_name, batcher, model_fp=args.model, epochs=args.epochs,
                 class_weight=class_weight, n_mini_epochs=args.mini_epochs)
