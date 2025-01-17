import logging
import time
import numpy as np
from tensorflow.python.training import moving_averages

import tensorflow.compat.v1 as tf
tf.disable_v2_behavior()
# import tensorflow as tf

TF_DTYPE = tf.float64
DELTA_CLIP = 100.0


class FeedForwardModel(object):
    """The fully connected neural network model."""
    def __init__(self, config, bsde, sess):
        self.eqn_config = config.eqn_config
        self.nn_config = config.nn_config
        self.problem_type = self.eqn_config.problem_type
        self.eigenpair = self.eqn_config.eigenpair
        self.model_type = self.eqn_config.model_type
        self.bsde = bsde
        self.sess = sess
        self.y_init = None
        # make sure consistent with FBSDE equation
        self.dim = bsde.dim
        self.num_time_interval = bsde.num_time_interval
        self.total_time = bsde.total_time
        # ops for statistics update of batch normalization
        self.extra_train_ops = []
        self.dw = tf.placeholder(TF_DTYPE, [None, self.dim, self.num_time_interval], name='dW')
        self.x = tf.placeholder(TF_DTYPE, [None, self.dim, self.num_time_interval + 1], name='X')
        self.is_training = tf.placeholder(tf.bool)
        self.train_loss, self.eigen_error, self.init_rel_loss, self.NN_consist,self.l2 = None, None, None, None, None
        self.init_infty_loss, self.grad_infty_loss = None, None
        self.train_ops, self.t_build = None, None
        self.train_ops_supervise, self.train_ops_supervise_approx, self.train_ops_fixEigen = None, None, None
        self.eigen = tf.get_variable(
            'eigen', shape=[1], dtype=TF_DTYPE,
            initializer=tf.random_uniform_initializer(self.eqn_config.initeigen_low, self.eqn_config.initeigen_high),
            trainable=True
        )
        self.x_hist = tf.placeholder(TF_DTYPE, [None, self.dim], name='x_hist')
        self.hist_NN = None
        self.hist_true = None
        self.y_second = None
        self.hist_size = 20000
        self.second = False
        if self.eigenpair == 'second':
            self.second = True
            print("Solving the second eigenpair.")
    
    def train(self):
        start_time = time.time()
        # to save iteration results
        training_history = []
        # for validation
        dw_valid, x_valid = self.bsde.sample_uniform(self.nn_config.valid_size)
        # can still use batch norm of samples in the validation phase
        feed_dict_valid = {self.dw: dw_valid, self.x: x_valid, self.is_training: False}
        # initialization
        self.sess.run(tf.global_variables_initializer())
        
        # begin sgd iteration
        for step in range(self.nn_config.num_iterations+1):
            if step % self.nn_config.logging_frequency == 0:
                train_loss, eigen_error, init_rel_loss, grad_error, NN_consist, l2, init_infty_loss, grad_infty_loss = self.sess.run(
                    [
                        self.train_loss, self.eigen_error, self.init_rel_loss, self.grad_error, 
                        self.NN_consist, self.l2, self.init_infty_loss, self.grad_infty_loss
                    ],
                    feed_dict=feed_dict_valid)
                elapsed_time = time.time()-start_time+self.t_build
                training_history.append(
                    [
                        step, train_loss, eigen_error, init_rel_loss, init_infty_loss, 
                        grad_error, grad_infty_loss, NN_consist, l2, elapsed_time
                    ]
                )
                if self.nn_config.verbose:
                    logging.info(
                        "step: %5u,    train_loss: %.4e,   eg_err: %.4e, egfcn_l2_err: %.4e, egfcn_infty_err: %.4e, grad_l2_err: %.4e " % (
                            step, train_loss, eigen_error, init_rel_loss, init_infty_loss, grad_error) +
                        "grad_infty_err: %.4e, consist_loss: %.4e, norm_fac: %.4e, elapsed time %3u" % (
                         grad_infty_loss, NN_consist, l2, elapsed_time))
            dw_train, x_train = self.bsde.sample_uniform(self.nn_config.batch_size)
            if self.second:
                if self.eqn_config.pretrain == "degenerate": # for dwclose_d1
                    if step < 1000: # use approximate solution to do supervised learning
                        self.sess.run(self.train_ops_supervise_approx,feed_dict={self.dw: dw_train, self.x: x_train, self.is_training: True})
                    else: # use the default loss function
                        self.sess.run(self.train_ops,feed_dict={self.dw: dw_train, self.x: x_train, self.is_training: True})
                else: # self.eqn_config.second marks the step to change loss function
                    if step < self.eqn_config.pretrain:
                        self.sess.run(self.train_ops_fixEigen,feed_dict={self.dw: dw_train, self.x: x_train, self.is_training: True})
                    else:
                        self.sess.run(self.train_ops,feed_dict={self.dw: dw_train, self.x: x_train, self.is_training: True})
            else:
                self.sess.run(self.train_ops,feed_dict={self.dw: dw_train, self.x: x_train, self.is_training: True})
            if step == self.nn_config.num_iterations:
                x_hist = self.bsde.sample_hist(self.hist_size)
                feed_dict_hist = {self.x_hist: x_hist}
                if self.second:
                    [y_second, y_hist] = self.sess.run([self.y_second, self.hist_NN], feed_dict=feed_dict_hist)
                    figure_data = np.concatenate([y_second, y_hist], axis=1)
                else:
                    [y_hist_true, y_hist] = self.sess.run([self.hist_true, self.hist_NN], feed_dict=feed_dict_hist)
                    figure_data = np.concatenate([y_hist_true, y_hist], axis=1)
        if self.dim == 1:
            figure_data = np.concatenate([x_hist,figure_data], axis=1)
        return np.array(training_history, dtype=object), figure_data

    def build(self):
        start_time = time.time()
        with tf.variable_scope('forward'):
            global_step = tf.get_variable('global_step', [],
                                          initializer=tf.constant_initializer(0),
                                          trainable=False, dtype=tf.int32)
            decay = tf.train.piecewise_constant(
                global_step, self.nn_config.ma_boundaries,
                [tf.constant(ma, dtype=TF_DTYPE) for ma in self.nn_config.ma_values])
            x_init = self.x[:, :, 0]
            net_y = PeriodNet(self.nn_config.num_hiddens, out_dim=1,
                              trig_order=self.nn_config.trig_order, name='net_y')
            if self.model_type == 'consistent':
                net_z = PeriodNet(self.nn_config.num_hiddens, out_dim=self.dim,
                                  trig_order=self.nn_config.trig_order, name='net_z')
                
            y_init_and_gradient = net_y(x_init,need_grad=True)
            y_init = y_init_and_gradient[0]
            
            yl2_batch = tf.reduce_mean(y_init ** 2)
            yl2_ma = tf.get_variable(
                'yl2_ma', [1], TF_DTYPE,
                initializer=tf.constant_initializer(100.0, TF_DTYPE),
                trainable=False)
            yl2 = tf.cond(self.is_training,
                          lambda: decay * yl2_ma + (1 - decay) * yl2_batch,
                          lambda: yl2_ma)
            true_z = self.bsde.true_z(x_init)
            
            sign = tf.sign(tf.reduce_sum(y_init))
            
            if self.model_type == 'consistent':
                z = net_z(x_init, need_grad=False)
            else:
                z = y_init_and_gradient[1]
                z = z / tf.sqrt(yl2) * sign
            
            normed_true_z = true_z / tf.sqrt(tf.reduce_mean(true_z ** 2))
            error_z = z / tf.sqrt(tf.reduce_mean(z ** 2)) - normed_true_z
            y_init = y_init / tf.sqrt(yl2) * sign
            
            if self.model_type == 'consistent':
                x_T = self.x[:, :, -1]
                z_T = net_z(x_T, need_grad=False)
                yT_and_gradient = net_y(x_T,need_grad=True)
                grad_yT = yT_and_gradient[1]
                grad_yT = grad_yT * sign / tf.sqrt(yl2)
                if self.problem_type == 'nonlinear':
                    grad_yT = grad_yT * self.bsde.L2mean
                NN_consist = z_T - grad_yT
            else:
                NN_consist = tf.constant(0.0, dtype = TF_DTYPE)
            
            y = y_init
            if self.problem_type == 'nonlinear':
                y = y * self.bsde.L2mean
            for t in range(0, self.num_time_interval-1):
                y = y - self.bsde.delta_t * (self.bsde.f_tf(self.x[:, :, t], y, z) + self.eigen *y) + \
                    tf.reduce_sum(z * self.dw[:, :, t], 1, keepdims=True)
                if self.problem_type == 'nonlinear':
                    y = tf.clip_by_value(y, -5, 5, name=None)
                if self.model_type == 'consistent':
                    z = net_z(self.x[:, :, t + 1], need_grad=False, reuse=True)
                else:
                    yz = net_y(self.x[:, :, t + 1], need_grad=True, reuse=True)
                    z = yz[1] / tf.sqrt(yl2) * sign
            # terminal time
            y = y - self.bsde.delta_t * (self.bsde.f_tf(self.x[:, :, -2], y, z) + self.eigen *y) + \
                tf.reduce_sum(z * self.dw[:, :, -1], 1, keepdims=True)
            y_xT = net_y(self.x[:, :, -1], need_grad=False, reuse=True)
            y_xT = y_xT / tf.sqrt(yl2) * sign
            if self.problem_type == 'nonlinear':
                y_xT = y_xT * self.bsde.L2mean
            delta = y - y_xT
            
            # use linear approximation outside the clipped range
            self.train_loss = tf.reduce_mean(
                tf.where(tf.abs(delta) < DELTA_CLIP, tf.square(delta),
                         2 * DELTA_CLIP * tf.abs(delta) - DELTA_CLIP ** 2)) * 1000 \
                    + tf.reduce_mean(tf.square(NN_consist)) * 20\
                    + tf.nn.relu(2 - yl2) * 100
            if self.problem_type == 'nonlinear':
                self.train_loss += tf.nn.relu(2 * self.bsde.L2mean - yl2) * 100
            self.extra_train_ops.append(
                moving_averages.assign_moving_average(yl2_ma, yl2_batch, decay))
            y_hist = net_y(self.x_hist, need_grad=False, reuse=True)
            hist_sign = tf.sign(tf.reduce_sum(y_hist))
            hist_l2 = tf.reduce_mean(y_hist ** 2)
            self.hist_NN = y_hist / tf.sqrt(hist_l2) * hist_sign
            hist_true_y = self.bsde.true_y(self.x_hist)
            self.hist_true = hist_true_y / tf.sqrt(tf.reduce_mean(hist_true_y ** 2))
        true_init = self.bsde.true_y(self.x[:, :, 0])
        true_init = true_init / tf.sqrt(tf.reduce_mean(true_init ** 2))
        error_y = y_init - true_init
        self.init_rel_loss = tf.sqrt(tf.reduce_mean(error_y ** 2))
        self.init_infty_loss = tf.reduce_max(tf.abs(error_y))
        self.eigen_error = self.eigen - self.bsde.true_eigen
        self.l2 = yl2
        self.grad_error = tf.sqrt(tf.reduce_mean(error_z ** 2))
        self.grad_infty_loss = tf.reduce_max(tf.abs(error_z))
        self.NN_consist = tf.sqrt(tf.reduce_mean(NN_consist ** 2))
        
        # train operations
        learning_rate = tf.train.piecewise_constant(global_step,
                                                    self.nn_config.lr_boundaries,
                                                    self.nn_config.lr_values)
        trainable_variables = tf.trainable_variables()
        grads = tf.gradients(self.train_loss, trainable_variables)
        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
        apply_op = optimizer.apply_gradients(zip(grads, trainable_variables),
                                             global_step=global_step, name='train_step')
        all_ops = [apply_op] + self.extra_train_ops
        self.train_ops = tf.group(*all_ops)
        self.t_build = time.time() - start_time

    def build_second(self):
        start_time = time.time()
        with tf.variable_scope('forward'):
            global_step = tf.get_variable('global_step', [],
                                          initializer=tf.constant_initializer(0),
                                          trainable=False, dtype=tf.int32)
            decay = tf.train.piecewise_constant(
                global_step, self.nn_config.ma_boundaries,
                [tf.constant(ma, dtype=TF_DTYPE) for ma in self.nn_config.ma_values])
            x_init = self.x[:, :, 0]
            net_y = PeriodNet(self.nn_config.num_hiddens, out_dim=1,
                              trig_order=self.nn_config.trig_order, name='net_y')
            net_z = PeriodNet(self.nn_config.num_hiddens, out_dim=self.dim,
                              trig_order=self.nn_config.trig_order, name='net_z')
            y_init_and_gradient = net_y(x_init,need_grad=True)
            y_init = y_init_and_gradient[0]
            grad_y = y_init_and_gradient[1]
            z = net_z(x_init, need_grad=False)
            z_init = z
            
            yl2_batch = tf.reduce_mean(y_init ** 2)
            yl2_ma = tf.get_variable(
                'yl2_ma', [1], TF_DTYPE,
                initializer=tf.constant_initializer(100.0, TF_DTYPE),
                trainable=False)
            yl2 = tf.cond(self.is_training,
                          lambda: decay * yl2_ma + (1 - decay) * yl2_batch,
                          lambda: yl2_ma)
            true_z = self.bsde.true_z(x_init)
            
            second_z = self.bsde.second_z(x_init)
            normed_second_z = second_z / tf.sqrt(tf.reduce_mean(second_z ** 2))
            error_second_z = z / tf.sqrt(tf.reduce_mean(z ** 2)) - normed_second_z
            error_second_z2 = z / tf.sqrt(tf.reduce_mean(z ** 2)) + normed_second_z
            
            normed_true_z = true_z / tf.sqrt(tf.reduce_mean(true_z ** 2))
            error_z = z / tf.sqrt(tf.reduce_mean(z ** 2)) - normed_true_z
            
            y_init = y_init / tf.sqrt(yl2)
            grad_y = grad_y / tf.sqrt(yl2)
            
            x_T = self.x[:, :, -1]
            z_T = net_z(x_T, need_grad=False)
            yT_and_gradient = net_y(x_T,need_grad=True)
            grad_yT = yT_and_gradient[1]
            grad_yT = grad_yT / tf.sqrt(yl2)
            NN_consist = z_T - grad_yT
            
            y = y_init
            for t in range(0, self.num_time_interval-1):
                y = y - self.bsde.delta_t * (self.bsde.f_tf(self.x[:, :, t], y, z) + self.eigen *y) + \
                    tf.reduce_sum(z * self.dw[:, :, t], 1, keepdims=True)
                z = net_z(self.x[:, :, t + 1], need_grad=False, reuse=True)
            # terminal time
            y = y - self.bsde.delta_t * (self.bsde.f_tf(self.x[:, :, -2], y, z) + self.eigen *y) + \
                tf.reduce_sum(z * self.dw[:, :, -1], 1, keepdims=True)
            y_xT = net_y(self.x[:, :, -1], need_grad=False, reuse=True)
            y_xT = y_xT / tf.sqrt(yl2)
            delta = y - y_xT
            
            # use linear approximation outside the clipped range
            self.train_loss_supervise_approx = \
                tf.reduce_mean((y_init_and_gradient[0] - self.bsde.second_y_approx(x_init))**2) \
                    + 0*tf.reduce_mean(NN_consist**2) \
                    + tf.reduce_mean((z_init - self.bsde.second_z_approx(x_init))**2) \
                    + (self.eigen - self.bsde.second_eigen)**2
            self.train_loss_fixEigen = tf.reduce_mean(
                tf.where(tf.abs(delta) < DELTA_CLIP, tf.square(delta),
                         2 * DELTA_CLIP * tf.abs(delta) - DELTA_CLIP ** 2)) * 1000 \
                    + ((self.eigen - self.bsde.second_eigen - 0.1)**2) * 1000000 \
                    + tf.reduce_mean(tf.square(NN_consist)) * 20\
                    + tf.nn.relu(2 - yl2) * 100
            self.train_loss = tf.reduce_mean(
                tf.where(tf.abs(delta) < DELTA_CLIP, tf.square(delta),
                         2 * DELTA_CLIP * tf.abs(delta) - DELTA_CLIP ** 2)) * 1000 \
                    + tf.reduce_mean(tf.square(NN_consist)) * 20\
                    + tf.nn.relu(2 - yl2) * 100
            self.extra_train_ops.append(
                moving_averages.assign_moving_average(yl2_ma, yl2_batch, decay))
            y_hist = net_y(self.x_hist, need_grad=False, reuse=True)
            hist_l2 = tf.reduce_mean(y_hist ** 2)
            self.hist_NN = y_hist / tf.sqrt(hist_l2)
            if not self.second:
                hist_sign = tf.sign(tf.reduce_sum(y_hist))
                self.hist_NN = self.hist_NN * hist_sign
            hist_true_y = self.bsde.true_y(self.x_hist)
            self.hist_true = hist_true_y / tf.sqrt(tf.reduce_mean(hist_true_y ** 2))
            y_second = self.bsde.second_y(self.x_hist)
            self.y_second = y_second / tf.sqrt(tf.reduce_mean(y_second ** 2))
        second_init = self.bsde.second_y(self.x[:, :, 0])
        second_init = second_init / tf.sqrt(tf.reduce_mean(second_init ** 2))
        error_y_second = y_init - second_init
        self.eigen_error = self.eigen - self.bsde.second_eigen
        self.init_rel_loss = tf.sqrt(tf.reduce_mean(error_y_second ** 2))
        self.init_infty_loss = tf.reduce_max(tf.abs(error_y_second))
        self.l2 = yl2
        self.grad_error = tf.sqrt(tf.reduce_mean(error_second_z ** 2))
        self.grad_infty_loss = tf.reduce_max(tf.abs(error_second_z))
        self.NN_consist = tf.sqrt(tf.reduce_mean(NN_consist ** 2))
        # the following is to overcome the sign problem, useful when net_y is not wel initialized
        error_y_second2 = y_init + second_init
        init_rel_loss2 = tf.sqrt(tf.reduce_mean(error_y_second2 ** 2))
        init_infty_loss2 = tf.reduce_max(tf.abs(error_y_second2))
        grad_error2 = tf.sqrt(tf.reduce_mean(error_second_z2 ** 2))
        grad_infty_loss2 = tf.reduce_max(tf.abs(error_second_z2))
        self.init_rel_loss = tf.minimum(self.init_rel_loss, init_rel_loss2)
        self.init_infty_loss = tf.minimum(self.init_infty_loss, init_infty_loss2)
        self.grad_error = tf.minimum(self.grad_error, grad_error2)
        self.grad_infty_loss = tf.minimum(self.grad_infty_loss, grad_infty_loss2)
        
        # train operations
        learning_rate = tf.train.piecewise_constant(global_step,
                                                    self.nn_config.lr_boundaries,
                                                    self.nn_config.lr_values)
        trainable_variables = tf.trainable_variables()
        grads = tf.gradients(self.train_loss, trainable_variables)
        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
        apply_op = optimizer.apply_gradients(zip(grads, trainable_variables),
                                             global_step=global_step, name='train_step')
        all_ops = [apply_op] + self.extra_train_ops
        self.train_ops = tf.group(*all_ops)
        
        grads_supervise_approx = tf.gradients(self.train_loss_supervise_approx, trainable_variables)
        optimizer_supervise_approx = tf.train.AdamOptimizer(learning_rate=learning_rate)
        apply_op_supervise_approx = optimizer_supervise_approx.apply_gradients(zip(grads_supervise_approx, trainable_variables),
                                             global_step=global_step, name='train_step')
        all_ops_supervise_approx = [apply_op_supervise_approx] + self.extra_train_ops
        self.train_ops_supervise_approx = tf.group(*all_ops_supervise_approx)
        
        grads_fixEigen = tf.gradients(self.train_loss_fixEigen, trainable_variables)
        optimizer_fixEigen = tf.train.AdamOptimizer(learning_rate=learning_rate)
        apply_op_fixEigen = optimizer_fixEigen.apply_gradients(zip(grads_fixEigen, trainable_variables),
                                             global_step=global_step, name='train_step')
        all_ops_fixEigen = [apply_op_fixEigen] + self.extra_train_ops
        self.train_ops_fixEigen = tf.group(*all_ops_fixEigen)
        
        self.t_build = time.time() - start_time

    def build_true(self):
        # both true_y and true_z are multiplied by trainable constant
        start_time = time.time()
        self._init_coef_y = tf.get_variable(
            'init_coef_y', [1], TF_DTYPE, initializer=tf.constant_initializer(1.0, TF_DTYPE),
            trainable=False)
        self._init_coef_z = tf.get_variable(
            'init_coef_z', [1], TF_DTYPE, initializer=tf.constant_initializer(1.0, TF_DTYPE),
            trainable=False)
        with tf.variable_scope('forward'):
            x_init = self.x[:, :, 0]
            y_init = self.bsde.true_y(x_init) * self._init_coef_y
            yl2 = tf.reduce_mean(y_init ** 2)
            y = y_init
            self.z_init = self.bsde.true_z(x_init) * self._init_coef_z
            z = self.z_init
            for t in range(0, self.num_time_interval - 1):
                y = y - self.bsde.delta_t * (self.bsde.f_tf(self.x[:, :, t], y, z) + self.eigen * y) + \
                    tf.reduce_sum(z * self.dw[:, :, t], 1, keepdims=True)
                z = self.bsde.true_z(self.x[:, :, t + 1]) * self._init_coef_z
            # terminal time
            y = y - self.bsde.delta_t * (self.bsde.f_tf(self.x[:, :, -2], y, z) + self.eigen * y) + \
                tf.reduce_sum(z * self.dw[:, :, -1], 1, keepdims=True)
            y_xT = self.bsde.true_y(self.x[:, :, -1])
            delta = y - y_xT
            # use linear approximation outside the clipped range
            self.train_loss = tf.reduce_mean(
                tf.where(tf.abs(delta) < DELTA_CLIP, tf.square(delta),
                          2 * DELTA_CLIP * tf.abs(delta) - DELTA_CLIP ** 2)) * 100
            hist_true_y = self.bsde.true_y(self.x_hist)
            self.hist_true = hist_true_y / tf.sqrt(tf.reduce_mean(hist_true_y ** 2))
            self.hist_NN = self.hist_true
        self.init_rel_loss = self._init_coef_y -1
        self.init_infty_loss = self._init_coef_y -1
        self.eigen_error = self.eigen - self.bsde.true_eigen
        self.l2 = yl2
        self.grad_error = self._init_coef_z -1
        self.grad_infty_loss = self._init_coef_z -1
        self.NN_consist = tf.constant(0)
        # train operations
        global_step = tf.get_variable('global_step', [],
                                      initializer=tf.constant_initializer(0),
                                      trainable=False, dtype=tf.int32)
        learning_rate = tf.train.piecewise_constant(global_step,
                                                    self.nn_config.lr_boundaries,
                                                    self.nn_config.lr_values)
        trainable_variables = tf.trainable_variables()
        grads = tf.gradients(self.train_loss, trainable_variables)
        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
        apply_op = optimizer.apply_gradients(zip(grads, trainable_variables),
                                             global_step=global_step, name='train_step')

        all_ops = [apply_op] + self.extra_train_ops
        self.train_ops = tf.group(*all_ops)
        self.t_build = time.time() - start_time
        

class PeriodNet(object):
    def __init__(self, num_hiddens, out_dim, trig_order, name='period_net'):
        self.num_hiddens = num_hiddens
        self.out_dim = out_dim
        self.trig_order = trig_order
        self.name = name

    def __call__(self, x, need_grad, reuse=tf.AUTO_REUSE):
        with tf.variable_scope(self.name):
            trig_bases = []
            for i in range(1, self.trig_order+1):
                trig_bases += [tf.sin(i * x), tf.cos(i * x)]
            trig_bases = tf.concat(trig_bases, axis=1)
            h = trig_bases
            for i in range(0, len(self.num_hiddens)):
                h = tf.layers.dense(h, self.num_hiddens[i], activation=tf.nn.relu,
                                    name="full%g" % (i + 1), reuse=reuse)
            u = tf.layers.dense(
                h, self.out_dim, activation=None, name="final_layer", reuse=reuse)
            if need_grad:
                # the shape of grad is 1 x num_sample x dim
                grad = tf.gradients(u, x)[0]
        if need_grad:
            return u, grad*np.sqrt(2)
        else:
            return u
