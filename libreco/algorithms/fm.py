"""Implementation of FM."""
from ..bases import ModelMeta, TfBase
from ..feature.multi_sparse import true_sparse_field_size
from ..layers import embedding_lookup, tf_dense
from ..tfops import dropout_config, reg_config, tf
from ..tfops.features import compute_dense_feats, compute_sparse_feats
from ..utils.misc import count_params
from ..utils.validate import (
    check_dense_values,
    check_multi_sparse,
    check_sparse_indices,
    dense_field_size,
    sparse_feat_size,
    sparse_field_size,
)


class FM(TfBase, metaclass=ModelMeta):
    """*Factorization Machines* algorithm.

    Note this implementation is actually a mixture of FM and NFM, since it uses one
    dense layer in the final output

    Parameters
    ----------
    task : {'rating', 'ranking'}
        Recommendation task. See :ref:`Task`.
    data_info : :class:`~libreco.data.DataInfo` object
        Object that contains useful information for training and inference.
    loss_type : {'cross_entropy', 'focal'}, default: 'cross_entropy'
        Loss for model training.
    embed_size: int, default: 16
        Vector size of embeddings.
    n_epochs: int, default: 10
        Number of epochs for training.
    lr : float, default 0.001
        Learning rate for training.
    lr_decay : bool, default: False
        Whether to use learning rate decay.
    epsilon : float, default: 1e-5
        A small constant added to the denominator to improve numerical stability in
        Adam optimizer.
        According to the `official comment <https://github.com/tensorflow/tensorflow/blob/v1.15.0/tensorflow/python/training/adam.py#L64>`_,
        default value of `1e-8` for `epsilon` is generally not good, so here we choose `1e-5`.
        Users can try tuning this hyperparameter if the training is unstable.
    reg : float or None, default: None
        Regularization parameter, must be non-negative or None.
    batch_size : int, default: 256
        Batch size for training.
    sampler : {'random', 'unconsumed', 'popular'}, default: 'random'
        Negative sampling strategy.

        - ``'random'`` means random sampling.
        - ``'unconsumed'`` samples items that the target user did not consume before.
        - ``'popular'`` has a higher probability to sample popular items as negative samples.

        .. versionadded:: 1.1.0

    num_neg : int, default: 1
        Number of negative samples for each positive sample, only used in `ranking` task.
    use_bn : bool, default: True
        Whether to use batch normalization.
    dropout_rate : float or None, default: None
        Probability of an element to be zeroed. If it is None, dropout is not used.
    multi_sparse_combiner : {'normal', 'mean', 'sum', 'sqrtn'}, default: 'sqrtn'
        Options for combining `multi_sparse` features.
    seed : int, default: 42
        Random seed.
    lower_upper_bound : tuple or None, default: None
        Lower and upper score bound for `rating` task.
    tf_sess_config : dict or None, default: None
        Optional TensorFlow session config, see `ConfigProto options
        <https://github.com/tensorflow/tensorflow/blob/v2.10.0/tensorflow/core/protobuf/config.proto#L431>`_.

    References
    ----------
    [1] *Steffen Rendle* `Factorization Machines
    <https://www.csie.ntu.edu.tw/~b97053/paper/Rendle2010FM.pdf>`_.

    [2] *Xiangnan He et al.* `Neural Factorization Machines for Sparse Predictive Analytics
    <https://arxiv.org/pdf/1708.05027.pdf>`_.
    """

    user_variables = ("embedding/user_linear_var", "embedding/user_embeds_var")
    item_variables = ("embedding/item_linear_var", "embedding/item_embeds_var")
    sparse_variables = ("embedding/sparse_linear_var", "embedding/sparse_embeds_var")
    dense_variables = ("embedding/dense_linear_var", "embedding/dense_embeds_var")

    def __init__(
        self,
        task,
        data_info,
        loss_type="cross_entropy",
        embed_size=16,
        n_epochs=20,
        lr=0.001,
        lr_decay=False,
        epsilon=1e-5,
        reg=None,
        batch_size=256,
        sampler="random",
        num_neg=1,
        use_bn=True,
        dropout_rate=None,
        multi_sparse_combiner="sqrtn",
        seed=42,
        lower_upper_bound=None,
        tf_sess_config=None,
    ):
        super().__init__(task, data_info, lower_upper_bound, tf_sess_config)

        self.all_args = locals()
        self.loss_type = loss_type
        self.embed_size = embed_size
        self.n_epochs = n_epochs
        self.lr = lr
        self.lr_decay = lr_decay
        self.epsilon = epsilon
        self.reg = reg_config(reg)
        self.batch_size = batch_size
        self.sampler = sampler
        self.num_neg = num_neg
        self.use_bn = use_bn
        self.dropout_rate = dropout_config(dropout_rate)
        self.seed = seed
        self.sparse = check_sparse_indices(data_info)
        self.dense = check_dense_values(data_info)
        if self.sparse:
            self.sparse_feature_size = sparse_feat_size(data_info)
            self.sparse_field_size = sparse_field_size(data_info)
            self.multi_sparse_combiner = check_multi_sparse(
                data_info, multi_sparse_combiner
            )
            self.true_sparse_field_size = true_sparse_field_size(
                data_info, self.sparse_field_size, self.multi_sparse_combiner
            )
        if self.dense:
            self.dense_field_size = dense_field_size(data_info)

    def build_model(self):
        tf.set_random_seed(self.seed)
        self.labels = tf.placeholder(tf.float32, shape=[None])
        self.is_training = tf.placeholder_with_default(False, shape=[])
        self.linear_embed, self.pairwise_embed = [], []

        self._build_user_item()
        if self.sparse:
            self._build_sparse()
        if self.dense:
            self._build_dense()

        linear_embed = tf.concat(self.linear_embed, axis=1)
        pairwise_embed = tf.concat(self.pairwise_embed, axis=1)

        # B * 1
        linear_term = tf_dense(units=1, activation=None)(linear_embed)
        # B * K
        pairwise_term = 0.5 * tf.subtract(
            tf.square(tf.reduce_sum(pairwise_embed, axis=1)),
            tf.reduce_sum(tf.square(pairwise_embed), axis=1),
        )

        # For original FM, just add K dim together:
        # pairwise_term = 0.5 * tf.reduce_sum(pairwise_term, axis=1)
        if self.use_bn:
            pairwise_term = tf.layers.batch_normalization(
                pairwise_term, training=self.is_training
            )
        pairwise_term = tf_dense(units=1, activation=tf.nn.elu)(pairwise_term)
        self.output = tf.squeeze(tf.add(linear_term, pairwise_term))
        self.serving_topk = self.build_topk(self.output)
        count_params()

    def _build_user_item(self):
        self.user_indices = tf.placeholder(tf.int32, shape=[None])
        self.item_indices = tf.placeholder(tf.int32, shape=[None])

        linear_user_embeds = embedding_lookup(
            indices=self.user_indices,
            var_name="user_linear_var",
            var_shape=(self.n_users + 1, 1),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        linear_item_embeds = embedding_lookup(
            indices=self.item_indices,
            var_name="item_linear_var",
            var_shape=(self.n_items + 1, 1),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        user_embeds = embedding_lookup(
            indices=self.user_indices,
            var_name="user_embeds_var",
            var_shape=(self.n_users + 1, self.embed_size),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        item_embeds = embedding_lookup(
            indices=self.item_indices,
            var_name="item_embeds_var",
            var_shape=(self.n_items + 1, self.embed_size),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        self.linear_embed.extend([linear_user_embeds, linear_item_embeds])
        self.pairwise_embed.extend(
            [user_embeds[:, tf.newaxis, :], item_embeds[:, tf.newaxis, :]]
        )

    def _build_sparse(self):
        self.sparse_indices = tf.placeholder(
            tf.int32, shape=[None, self.sparse_field_size]
        )
        linear_sparse_embed = compute_sparse_feats(
            self.data_info,
            self.multi_sparse_combiner,
            self.sparse_indices,
            var_name="sparse_linear_var",
            var_shape=[self.sparse_feature_size],
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        pairwise_sparse_embed = compute_sparse_feats(
            self.data_info,
            self.multi_sparse_combiner,
            self.sparse_indices,
            var_name="sparse_embeds_var",
            var_shape=(self.sparse_feature_size, self.embed_size),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        self.linear_embed.append(linear_sparse_embed)
        self.pairwise_embed.append(pairwise_sparse_embed)

    def _build_dense(self):
        self.dense_values = tf.placeholder(
            tf.float32, shape=[None, self.dense_field_size]
        )
        linear_dense_embed = compute_dense_feats(
            self.dense_values,
            var_name="dense_linear_var",
            var_shape=[self.dense_field_size],
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        pairwise_dense_embed = compute_dense_feats(
            self.dense_values,
            var_name="dense_embeds_var",
            var_shape=(self.dense_field_size, self.embed_size),
            initializer=tf.glorot_uniform_initializer(),
            regularizer=self.reg,
        )
        self.linear_embed.append(linear_dense_embed)
        self.pairwise_embed.append(pairwise_dense_embed)
