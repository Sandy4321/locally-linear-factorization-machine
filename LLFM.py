'''
Tensorflow implementation of Localized Factorization Machines

'''
import math
import numpy as np
import tensorflow as tf
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.metrics import mean_squared_error
from sklearn.metrics import log_loss
from time import time
import argparse
import LoadData_nonsparse as DATA
from tensorflow.contrib.layers.python.layers import batch_norm as batch_norm


#################### Arguments ####################
def parse_args():
    parser = argparse.ArgumentParser(description="Run FM.")
    parser.add_argument('--path', nargs='?', default='data/',
                        help='Input data path.')
    parser.add_argument('--dataset', nargs='?', default='banana',
                        help='Choose a dataset.')
    parser.add_argument('--epoch', type=int, default=1000,
                        help='Number of epochs.')
    parser.add_argument('--pretrain', type=int, default=-1,
                        help='flag for pretrain. 1: initialize from pretrain; 0: randomly initialize; -1: save the model to pretrain file')
    parser.add_argument('--batch_size', type=int, default=128,
                        help='Batch size.')
    parser.add_argument('--hidden_factor', type=int, default=64,
                        help='Number of hidden factors.')
    parser.add_argument('--anchor_points', type=int, default=10,
                        help='Number of anchor points')
    parser.add_argument('--regularization_factor', type=float, default=0,
                        help='Regularizer for bilinear part.')
    parser.add_argument('--keep_prob', type=float, default=1,
                        help='Keep probility (1-dropout_ratio) for the Bi-Interaction layer. 1: no dropout')
    parser.add_argument('--lr', type=float, default=0.001,
                        help='Learning rate.')
    parser.add_argument('--loss_type', nargs='?', default='log_loss',
                        help='Specify a loss type (square_loss or log_loss).')
    parser.add_argument('--optimizer', nargs='?', default='AdamOptimizer',
                        help='Specify an optimizer type (AdamOptimizer, AdagradOptimizer, GradientDescentOptimizer, MomentumOptimizer).')
    parser.add_argument('--verbose', type=int, default=1,
                        help='Show the results per X epochs (0, 1 ... any positive integer)')
    parser.add_argument('--batch_norm', type=int, default=0,
                        help='Whether to perform batch normaization (0 or 1)')

    return parser.parse_args()


class LLFM(BaseEstimator, TransformerMixin):
    def __init__(self, features_M, pretrain_flag, save_file, hidden_factor, anchor_points, loss_type, epoch, batch_size,
                 learning_rate,
                 lambda_bilinear, keep,
                 optimizer_type, batch_norm, verbose, random_seed=2016):
        """

        :param features_M: No. of features in the input data
        :param pretrain_flag:
        :param save_file:
        :param hidden_factor:
        :param anchor_points:
        :param loss_type:
        :param epoch:
        :param batch_size:
        :param learning_rate:
        :param lamda_bilinear:
        :param keep:
        :param optimizer_type:
        :param batch_norm:
        :param verbose:
        :param random_seed:
        """
        # bind params to class
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.hidden_factor = hidden_factor
        self.anchor_points = anchor_points
        self.save_file = save_file
        self.pretrain_flag = pretrain_flag
        self.loss_type = loss_type
        self.features_M = features_M
        self.lambda_bilinear = lambda_bilinear
        self.keep = keep
        self.epoch = epoch
        self.random_seed = random_seed
        self.optimizer_type = optimizer_type
        self.batch_norm = batch_norm
        self.verbose = verbose
        # performance of each epoch
        self.train_rmse, self.valid_rmse, self.test_rmse = [], [], []

        # init all variables in a tensorflow graph
        self._init_graph()

    def _init_graph(self):
        '''
        Init a tensorflow Graph containing: input data, variables, model, loss, optimizer
        '''
        self.graph = tf.Graph()
        with self.graph.as_default():  # , tf.device('/cpu:0'):
            # Set graph level random seed
            tf.set_random_seed(self.random_seed)
            # Input data.
            self.train_features = tf.placeholder(tf.float32, shape=[None, self.features_M])  # None * features_M
            self.train_labels = tf.placeholder(tf.float32, shape=[None, 1])  # None * 1
            self.dropout_keep = tf.placeholder(tf.float32)
            self.train_phase = tf.placeholder(tf.bool)

            # Variables.
            self.weights = self._initialize_weights()

            # Model.

            # coefficients
            self.X2 = tf.matmul(tf.reduce_sum(tf.square(self.train_features), 1, keep_dims=True), tf.ones([1, self.anchor_points]))
            self.Y2 = tf.matmul(tf.ones_like(self.train_labels, dtype=tf.float32),
                                tf.reduce_sum(tf.square(self.weights['anchor_points']), 0, keep_dims=True))
            self.XY = tf.matmul(self.train_features, self.weights['anchor_points'])
            self.distance = self.X2 + self.Y2 - 2 * self.XY
            self.distance = tf.sqrt(self.distance)
            self.distance = -10 * self.distance
            self.coefficient = tf.nn.softmax(self.distance)  # None * A
            tmp, _ = tf.nn.top_k(self.coefficient, 5, sorted=True)
            tmp = tmp[:, -1]
            print(tmp.shape)
            exit()

            # _________ sum_square part _____________
            # get the summed up embeddings of features.
            # Note: train_features must be a sparse, 0/1 matrix
            # nonzero_embeddings = tf.nn.embedding_lookup(self.weights['feature_embeddings'], self.train_features)
            # self.summed_features_emb = tf.reduce_sum(nonzero_embeddings, 1)  # None * K

            self.weights_reshape = tf.reshape(self.weights['feature_embeddings'], [self.features_M, self.hidden_factor * self.anchor_points])

            self.summed_features_emb = tf.reshape(tf.matmul(self.train_features,
                                            self.weights_reshape), [-1, self.hidden_factor, self.anchor_points])  # None * K * A
            # get the element-multiplication
            self.summed_features_emb_square = tf.square(self.summed_features_emb)  # None * K * A

            # _________ square_sum part _____________
            # self.squared_features_emb = tf.square(nonzero_embeddings)
            # self.squared_sum_features_emb = tf.reduce_sum(self.squared_features_emb, 1)  # None * K * A
            self.squared_sum_features_emb = tf.reshape(tf.matmul(tf.square(self.train_features),
                                                 tf.square(self.weights_reshape)), [-1, self.hidden_factor, self.anchor_points])

            # ________ FM __________
            self.FM = 0.5 * tf.subtract(self.summed_features_emb_square, self.squared_sum_features_emb)  # None * K * A
            if self.batch_norm:
                self.FM = self.batch_norm_layer(self.FM, train_phase=self.train_phase, scope_bn='bn_fm')

            # TODO: How to dropout in a non-NN structure?
            self.FM = tf.nn.dropout(self.FM, self.dropout_keep)  # dropout at the FM layer

            # _________out _________
            self.Bilinear = tf.multiply(tf.reduce_sum(self.FM, 1), self.coefficient)  # None * A
            self.Feature_bias = tf.multiply(tf.matmul(self.train_features, self.weights['feature_bias']), self.coefficient)  # None * A
            self.Bias = tf.multiply(tf.matmul(tf.ones_like(self.train_labels), self.weights['bias']), self.coefficient)  # None * A

            self.bilinear_reduce = tf.reduce_sum(self.Bilinear, 1)
            self.feature_bias_reduce = tf.reduce_sum(self.Feature_bias, 1)
            self.bias_reduce = tf.reduce_sum(self.Bias, 1)
            print(self.bilinear_reduce.shape)
            print(self.feature_bias_reduce.shape)
            print(self.bias_reduce.shape)
            self.out = tf.add_n([self.bilinear_reduce, self.feature_bias_reduce, self.bias_reduce])  # None * 1
            self.out = self.out[:, tf.newaxis]

            # Compute the loss.
            if self.loss_type == 'square_loss':
                if self.lambda_bilinear > 0:
                    self.loss = tf.nn.l2_loss(
                        tf.subtract(self.train_labels, self.out)) + tf.contrib.layers.l2_regularizer(
                        self.lambda_bilinear)(self.weights['feature_embeddings'])  # regulizer
                else:
                    self.loss = tf.nn.l2_loss(tf.subtract(self.train_labels, self.out))
            elif self.loss_type == 'log_loss':
                self.out = tf.sigmoid(self.out)
                if self.lambda_bilinear > 0:
                    self.loss = tf.losses.log_loss(self.train_labels, self.out, weights=1.0, epsilon=1e-07,
                                                   scope=None) + tf.contrib.layers.l2_regularizer(
                        self.lambda_bilinear)(self.weights['feature_embeddings'])  # regulizer
                else:
                    self.loss = tf.losses.log_loss(self.train_labels, self.out, weights=1.0, epsilon=1e-07,
                                                   scope=None)

            # Optimizer.
            if self.optimizer_type == 'AdamOptimizer':
                self.optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate, beta1=0.9, beta2=0.999,
                                                        epsilon=1e-8).minimize(self.loss)
            elif self.optimizer_type == 'AdagradOptimizer':
                self.optimizer = tf.train.AdagradOptimizer(learning_rate=self.learning_rate,
                                                           initial_accumulator_value=1e-8).minimize(self.loss)
            elif self.optimizer_type == 'GradientDescentOptimizer':
                self.optimizer = tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate).minimize(self.loss)
            elif self.optimizer_type == 'MomentumOptimizer':
                self.optimizer = tf.train.MomentumOptimizer(learning_rate=self.learning_rate, momentum=0.95).minimize(
                    self.loss)

            # init
            self.saver = tf.train.Saver()
            init = tf.global_variables_initializer()
            self.sess = tf.Session()
            self.sess.run(init)

            # number of params
            total_parameters = 0
            for variable in self.weights.values():
                shape = variable.get_shape()  # shape is an array of tf.Dimension
                variable_parameters = 1
                for dim in shape:
                    variable_parameters *= dim.value
                total_parameters += variable_parameters
            if self.verbose > 0:
                print "#params: %d" % total_parameters

    def _initialize_weights(self):
        """
        feature_embeddings: interaction term, [features_M, K]
        feature_bias: linear term, [features_M, 1]
        bias: constant term, [1, 1]
        :return:
        """
        all_weights = dict()
        if self.pretrain_flag > 0:
            weight_saver = tf.train.import_meta_graph(self.save_file + '.meta')
            pretrain_graph = tf.get_default_graph()
            feature_embeddings = pretrain_graph.get_tensor_by_name('feature_embeddings:0')
            feature_bias = pretrain_graph.get_tensor_by_name('feature_bias:0')
            bias = pretrain_graph.get_tensor_by_name('bias:0')
            with tf.Session() as sess:
                weight_saver.restore(sess, self.save_file)
                fe, fb, b = sess.run([feature_embeddings, feature_bias, bias])
            all_weights['feature_embeddings'] = tf.Variable(fe, dtype=tf.float32)
            all_weights['feature_bias'] = tf.Variable(fb, dtype=tf.float32)
            all_weights['bias'] = tf.Variable(b, dtype=tf.float32)
        else:
            all_weights['feature_embeddings'] = tf.Variable(
                tf.random_normal([self.features_M, self.hidden_factor, self.anchor_points], 0.0, 0.01),
                name='feature_embeddings')  # features_M * K * A
            all_weights['feature_bias'] = tf.Variable(
                tf.random_uniform([self.features_M, self.anchor_points], 0.0, 0.0),
                name='feature_bias')  # features_M * A
            all_weights['bias'] = tf.Variable(tf.random_uniform([1, self.anchor_points]), name='bias')  # 1 * A
            all_weights['anchor_points'] = tf.Variable(tf.random_uniform([self.features_M, self.anchor_points]), name='anchor_points')  # M * A

        return all_weights

    def batch_norm_layer(self, x, train_phase, scope_bn):
        # Note: the decay parameter is tunable
        bn_train = batch_norm(x, decay=0.9, center=True, scale=True, updates_collections=None,
                              is_training=True, reuse=None, trainable=True, scope=scope_bn)
        bn_inference = batch_norm(x, decay=0.9, center=True, scale=True, updates_collections=None,
                                  is_training=False, reuse=True, trainable=True, scope=scope_bn)
        z = tf.cond(train_phase, lambda: bn_train, lambda: bn_inference)
        return z

    def partial_fit(self, data):  # fit a batch
        feed_dict = {self.train_features: data['X'], self.train_labels: data['Y'], self.dropout_keep: self.keep,
                     self.train_phase: True}
        loss, opt = self.sess.run((self.loss, self.optimizer), feed_dict=feed_dict)
        return loss

    def get_random_block_from_data(self, data, batch_size):  # generate a random block of training data
        start_index = np.random.randint(0, data['Y'].shape[0] - batch_size)
        return {
            'X': data['X'][start_index:start_index + batch_size, :],
            'Y': data['Y'][start_index:start_index + batch_size, np.newaxis]
        }

    def shuffle_in_unison_scary(self, a, b):  # shuffle two lists simutaneously
        rng_state = np.random.get_state()
        np.random.shuffle(a)
        np.random.set_state(rng_state)
        np.random.shuffle(b)

    def train(self, Train_data, Validation_data, Test_data):  # fit a dataset
        # Check Init performance
        if self.verbose > 0:
            t2 = time()
            init_train = self.evaluate(Train_data)
            init_valid = self.evaluate(Validation_data)
            init_test = self.evaluate(Test_data)
            print("Init: \t train=%.4f, validation=%.4f, test=%.4f [%.1f s]" % (
                init_train, init_valid, init_test, time() - t2))

        for epoch in xrange(self.epoch):
            t1 = time()
            self.shuffle_in_unison_scary(Train_data['X'], Train_data['Y'])
            total_batch = int(len(Train_data['Y']) / self.batch_size)
            for i in xrange(total_batch):
                # generate a batch
                batch_xs = self.get_random_block_from_data(Train_data, self.batch_size)
                # Fit training
                self.partial_fit(batch_xs)
            t2 = time()

            # output validation
            train_result = self.evaluate(Train_data)
            valid_result = self.evaluate(Validation_data)
            test_result = self.evaluate(Test_data)

            self.train_rmse.append(train_result)
            self.valid_rmse.append(valid_result)
            self.test_rmse.append(test_result)
            if self.verbose > 0 and epoch % self.verbose == 0:
                print("Epoch %d [%.1f s]\ttrain=%.4f, validation=%.4f, test=%.4f [%.1f s]"
                      % (epoch + 1, t2 - t1, train_result, valid_result, test_result, time() - t2))
                # if self.eva_termination(self.valid_rmse):
                #     break

        if self.pretrain_flag < 0:
            print "Save model to file as pretrain."
            # self.saver.save(self.sess, self.save_file)

    def eva_termination(self, valid):
        if self.loss_type == 'square_loss':
            if len(valid) > 5:
                if valid[-1] > valid[-2] and valid[-2] > valid[-3] and valid[-3] > valid[-4] and valid[-4] > valid[-5]:
                    return True
        else:
            if len(valid) > 5:
                if valid[-1] < valid[-2] and valid[-2] < valid[-3] and valid[-3] < valid[-4] and valid[-4] < valid[-5]:
                    return True
        return False

    def evaluate(self, data):  # evaluate the results for an input set
        num_example = data['Y'].shape[0]
        feed_dict = {self.train_features: data['X'], self.train_labels: [[y] for y in data['Y']],
                     self.dropout_keep: 1.0, self.train_phase: False}
        predictions = self.sess.run((self.out), feed_dict=feed_dict)
        y_pred = np.reshape(predictions, (num_example,))
        y_true = np.reshape(data['Y'], (num_example,))
        if self.loss_type == 'square_loss':
            predictions_bounded = np.maximum(y_pred, np.ones(num_example) * min(y_true))  # bound the lower values
            predictions_bounded = np.minimum(predictions_bounded,
                                             np.ones(num_example) * max(y_true))  # bound the higher values
            RMSE = math.sqrt(mean_squared_error(y_true, predictions_bounded))
            return RMSE
        elif self.loss_type == 'log_loss':
            logloss = log_loss(y_true, y_pred)  # I haven't checked the log_loss
            y_pred[y_pred > 0.499] = 1
            y_pred[y_pred < 0.5] = 0
            y_pred = y_pred.astype(dtype=np.int32)
            y_true = y_true.astype(dtype=np.int32)
            return np.sum(y_pred == y_true) / (num_example * 1.0)


if __name__ == '__main__':
    # Data loading
    args = parse_args()
    data = DATA.LoadData(args.path, args.dataset, args.loss_type, True)
    if args.verbose > 0:
        print(
            "FM: dataset=%s, factors=%d, loss_type=%s, #epoch=%d, batch=%d, lr=%.4f, lambda=%.1e, keep=%.2f, optimizer=%s, batch_norm=%d"
            % (args.dataset, args.hidden_factor, args.loss_type, args.epoch, args.batch_size, args.lr,
               args.regularization_factor, args.keep_prob, args.optimizer, args.batch_norm))

    save_file = './pretrain/%s_%d/%s_%d' % (args.dataset, args.hidden_factor, args.dataset, args.hidden_factor)
    # Training
    t1 = time()
    model = LLFM(data.features_M, args.pretrain, save_file, args.hidden_factor, args.anchor_points, args.loss_type,
                 args.epoch,
                 args.batch_size, args.lr, args.regularization_factor, args.keep_prob, args.optimizer, args.batch_norm,
                 args.verbose)
    model.train(data.Train_data, data.Validation_data, data.Test_data)

    # Find the best validation result across iterations
    best_valid_score = 0
    if args.loss_type == 'square_loss':
        best_valid_score = min(model.valid_rmse)
    elif args.loss_type == 'log_loss':
        best_valid_score = max(model.valid_rmse)
    best_epoch = model.valid_rmse.index(best_valid_score)
    print ("Best Iter(validation)= %d\t train = %.4f, valid = %.4f, test = %.4f [%.1f s]"
           % (best_epoch + 1, model.train_rmse[best_epoch], model.valid_rmse[best_epoch], model.test_rmse[best_epoch],
              time() - t1))
