# wrapping the pyro-based density estimator in a sklearn-like API
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

import pyro.distributions as dist
import pyro.distributions.transforms as T

from functools import partial
from torch.distributions import constraints
from pyro.nn import ConditionalAutoRegressiveNN

from os import makedirs
from os.path import join
from sklearn.model_selection import train_test_split
from tqdm import tqdm


class ConditionalNormalizingFlow:
    """Conditional normalizing flow based on pyro but wrapped such that it
    mimicks the scikit-learn API, using numpy arrays as inputs and outputs.
    """
    def __init__(self, save_path=None,
                 model_type="MAF", transform="Affine", optimizer="Adam",
                 num_inputs=4, num_cond_inputs=1, num_blocks=15,
                 num_hidden=128, activation_function="relu",
                 pre_exp_tanh=False, batch_norm=True, batch_norm_momentum=0.1,
                 clips_logscale=[-3, 3], use_stable=False, lr=0.0001,
                 weight_decay=0.000001, early_stopping=False,
                 n_epoch_no_change=10, no_gpu=False):

        if model_type != "MAF":
            raise NotImplementedError
        if transform != "Affine":
            raise NotImplementedError
        if optimizer != "Adam":
            raise NotImplementedError

        self.save_path = save_path
        self.de_model_path = join(save_path, "DE_models/")
        self.device = torch.device("cuda:0" if torch.cuda.is_available()
                                   and not no_gpu else "cpu")
        self.early_stopping = early_stopping
        self.n_epoch_no_change = n_epoch_no_change

        transforms = []

        try:
            iter(num_hidden)
        except TypeError:
            num_hidden = [num_hidden]

        if activation_function == "relu":
            nonlinearity = nn.ReLU()
        else:
            raise NotImplementedError(
                "Only ReLU activation function is currently supported")

        for i in range(num_blocks):
            # switch around order in every layer
            if i % 2 == 0:
                perm = list(range(num_inputs))
            else:
                perm = list(range(num_inputs))[::-1]

            if pre_exp_tanh:
                af = TanhConditionalAffineAutoregressive(
                    ConditionalAutoRegressiveNN(
                        num_inputs,
                        num_cond_inputs,
                        num_hidden,
                        permutation=torch.Tensor(perm),
                        nonlinearity=nonlinearity)
                )
            else:
                af = T.ConditionalAffineAutoregressive(
                    ConditionalAutoRegressiveNN(
                        num_inputs,
                        num_cond_inputs,
                        num_hidden,
                        permutation=torch.Tensor(perm),
                        nonlinearity=nonlinearity
                        ),
                    log_scale_min_clip=clips_logscale[0],
                    log_scale_max_clip=clips_logscale[1],
                    stable=use_stable
                    )
            transforms.append(af)

            if batch_norm:
                # no batchnorm in final layer
                if i == num_blocks-1:
                    continue

                bn = T.BatchNorm(num_inputs,
                                 momentum=batch_norm_momentum)
                transforms.append(bn)

        self.model = MAF_pyro(num_inputs, num_cond_inputs,
                              self.device, transforms)

        self.model.to(self.device)

        total_parameters = sum(
            p.numel() for p in self.model.parameters() if p.requires_grad)

        print(f"Normalizing Flow has {int(total_parameters)} parameters")

        self.optimizer = optim.Adam(self.model.parameters(),
                                    lr=lr, weight_decay=weight_decay)

        # defaulting to eval mode, switching to train mode in fit()
        self.model.eval()

    def fit(self, X, m, X_val=None, m_val=None, val_split=0.2,
            batch_size=256, epochs=100, verbose=False):

        assert not (epochs is None and not self.early_stopping), (
            "A finite number of epochs must be set if early stopping"
            " is not used!")

        # allowing not to provide validation set, just for compatibility with
        # the sklearn API
        if X_val is None and m_val is None:
            if val_split is None or not (val_split > 0. and val_split < 1.):
                raise ValueError("val_split is needs to be provided and lie "
                                 "between 0 and 1 in case X_val and m_val are "
                                 "not provided!")
            else:
                X_train, X_val, m_train, m_val = train_test_split(
                    X, m, test_size=val_split, shuffle=True)
        else:
            X_train = X.copy()
            m_train = m.copy()

        makedirs(self.de_model_path, exist_ok=True)

        nan_mask = ~np.isnan(X_train).any(axis=1)
        X_train = X_train[nan_mask]
        m_train = m_train[nan_mask]

        nan_mask = ~np.isnan(X_val).any(axis=1)
        X_val = X_val[nan_mask]
        m_val = m_val[nan_mask]

        # build data loader out of numpy arrays
        train_loader = numpy_to_torch_loader(X_train, m_train,
                                             batch_size=batch_size,
                                             shuffle=True,
                                             device=self.device)
        val_loader = numpy_to_torch_loader(X_val, m_val,
                                           batch_size=batch_size,
                                           shuffle=True,
                                           device=self.device)

        # record also untrained losses
        train_loss = compute_loss_over_batches(self.model, train_loader,
                                               device=self.device)[0]
        val_loss = compute_loss_over_batches(self.model, val_loader,
                                             device=self.device)[0]
        train_losses = np.array([train_loss])
        val_losses = np.array([val_loss])
        if self.save_path is not None:
            np.save(self._train_loss_path(), train_losses)
            np.save(self._val_loss_path(), val_losses)

        # training loop
        for epoch in range(epochs):
            print('\nEpoch: {}'.format(epoch))

            train_loss = train_epoch(self.model, self.optimizer, train_loader,
                                     device=self.device,
                                     verbose=verbose)[0]
            val_loss = compute_loss_over_batches(self.model, val_loader,
                                                 device=self.device)[0]

            print("train_loss = ", train_loss)
            print("val_loss = ", val_loss)
            train_losses = np.concatenate(
                (train_losses, np.array([train_loss])))
            val_losses = np.concatenate(
                (val_losses, np.array([val_loss])))

            if self.save_path is not None:
                np.save(self._train_loss_path(), train_losses)
                np.save(self._val_loss_path(), val_losses)
                self._save_model(self._model_path(epoch))

            if self.early_stopping:
                if epoch > self.n_epoch_no_change:
                    if np.all(val_losses[-self.n_epoch_no_change:] >
                              val_losses[-self.n_epoch_no_change - 1]):
                        print("Early stopping at epoch", epoch)
                        break

        self.load_best_model()

    def transform(self, X, m=None):

        # m needs to be provided, but trying to mimick the sklearn API here
        if m is None:
            raise ValueError("m needs to be provided!")

        X_torch = torch.from_numpy(X).type(torch.FloatTensor).to(self.device)
        m_torch = torch.from_numpy(m).type(torch.FloatTensor).to(self.device)
        Z = self.model(X_torch, m_torch)[0]

        return Z.cpu().detach().numpy()

    def sample(self, n_samples=1, m=None):

        # m needs to be provided, but trying to mimick the sklearn API here
        if m is None:
            raise ValueError("m needs to be provided!")

        m_torch = torch.from_numpy(m).type(torch.FloatTensor).to(self.device)
        X_torch = self.model.sample(num_samples=n_samples,
                                    cond_inputs=m_torch)
        return X_torch.cpu().detach().numpy()

    def predict_log_proba(self, X, m=None):

        # m needs to be provided, but trying to mimick the sklearn API here
        if m is None:
            raise ValueError("m needs to be provided!")

        X_torch = torch.from_numpy(X).type(torch.FloatTensor).to(self.device)
        m_torch = torch.from_numpy(m).type(torch.FloatTensor).to(self.device)
        log_prob = self.model.log_probs(X_torch, m_torch)
        return log_prob.detach().cpu().numpy().flatten()

    def predict_proba(self, X, m=None):
        return np.exp(self.predict_log_proba(X, m=m))

    def score_samples(self, X, m=None):
        return self.predict_log_proba(X, m=m)

    def score(self, X, m=None):
        return self.score_samples(X, m=m).sum()

    def load_best_model(self):
        val_losses = self.load_val_loss()
        best_epoch = np.argmin(val_losses) - 1  # includes pre-training loss
        self.load_epoch_model(best_epoch)
        self.model.eval()

    def load_train_loss(self):
        if self.save_path is None:
            raise ValueError("save_path is None, cannot load train loss")
        return np.load(self._train_loss_path())

    def load_val_loss(self):
        if self.save_path is None:
            raise ValueError("save_path is None, cannot load val loss")
        return np.load(self._val_loss_path())

    def load_epoch_model(self, epoch):
        self._load_model(self._model_path(epoch))

    def _load_model(self, model_path):
        self.model.load_state_dict(torch.load(model_path,
                                              map_location=self.device))

    def _save_model(self, model_path):
        torch.save(self.model.state_dict(), model_path)

    def _train_loss_path(self):
        return join(self.save_path, "DE_train_losses.npy")

    def _val_loss_path(self):
        return join(self.save_path, "DE_val_losses.npy")

    def _model_path(self, epoch):
        return join(self.de_model_path, f"DE_epoch_{epoch}.par")


# utility functions

def numpy_to_torch_loader(X, m, batch_size=256, shuffle=True,
                          device=torch.device("cpu")):
    X_torch = torch.from_numpy(X).type(torch.FloatTensor).to(device)
    m_torch = torch.from_numpy(m).type(torch.FloatTensor).to(device)
    dataset = torch.utils.data.TensorDataset(X_torch, m_torch)
    dataloader = torch.utils.data.DataLoader(
        dataset, batch_size=batch_size, shuffle=shuffle)
    return dataloader


def compute_loss_over_batches(model, data_loader, device=torch.device("cpu")):
    # for computing the averaged loss over the entire dataset.
    # Mainly useful for tracking losses during training
    model.eval()
    with torch.no_grad():
        now_loss = 0
        n_nans = 0
        n_highs = 0
        for batch_idx, batch_data in enumerate(data_loader):

            data = batch_data[0]
            data = data.to(device)
            cond_data = batch_data[1].float()
            cond_data = cond_data.to(device)

            loss_vals_raw = model.log_probs(data, cond_data)
            loss_vals = loss_vals_raw.flatten()

            n_nans += sum(torch.isnan(loss_vals)).item()
            n_highs += sum(torch.abs(loss_vals) >= 1000).item()
            loss_vals = loss_vals[~torch.isnan(loss_vals)]
            loss_vals = loss_vals[torch.abs(loss_vals) < 1000]
            loss = -loss_vals.mean()
            loss = loss.item()

            now_loss += loss
            end_loss = now_loss / (batch_idx + 1)
        print("n_nans =", n_nans)
        print("n_highs =", n_highs)
        return (end_loss, )


def train_epoch(model, optimizer, data_loader,
                device=torch.device("cpu"), verbose=True):
    # Does one epoch of flow model training.

    model.train()
    train_loss = 0
    train_loss_avg = []
    if verbose:
        pbar = tqdm(total=len(data_loader.dataset))
    for batch_idx, data in enumerate(data_loader):
        if isinstance(data, list):
            if len(data) > 1:
                cond_data = data[1].float()
                cond_data = cond_data.to(device)
            else:
                cond_data = None
            data = data[0]
        data = data.to(device)

        optimizer.zero_grad()
        loss = -model.log_probs(data, cond_data)
        train_loss += loss.mean().item()
        train_loss_avg.extend(loss.tolist())
        loss.mean().backward()
        optimizer.step()

        if verbose:
            pbar.update(data.size(0))
            pbar.set_description(
                "Train, Log likelihood in nats: {:.6f}".format(
                    -train_loss / (batch_idx + 1)))

    if verbose:
        pbar.close()

    return (np.array(train_loss_avg).flatten().mean(), )


class TanhConditionalAffineAutoregressive(T.ConditionalAffineAutoregressive):

    domain = constraints.real_vector
    codomain = constraints.real_vector
    bijective = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def condition(self, context):
        """
        Conditions on a context variable, returning a non-conditional transform
        of type :class:`~pyro.distributions.transforms.AffineAutoregressive`.
        """

        cond_nn = partial(self.nn, context=context)
        cond_nn.permutation = cond_nn.func.permutation
        cond_nn.get_permutation = cond_nn.func.get_permutation
        return TanhAffineAutoregressive(cond_nn, **self.kwargs)


class TanhAffineAutoregressive(T.AffineAutoregressive):

    domain = constraints.real_vector
    codomain = constraints.real_vector
    bijective = True
    sign = +1
    autoregressive = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.tanh = nn.Tanh()

    def _call(self, x):
        mean, log_scale = self.arn(x)
        self._cached_log_scale = log_scale
        scale = torch.exp(self.tanh(log_scale))

        y = scale * x + mean
        return y

    def _inverse(self, y):
        x_size = y.size()[:-1]
        perm = self.arn.permutation
        input_dim = y.size(-1)
        x = [torch.zeros(x_size, device=y.device)] * input_dim

        # NOTE: Inversion is an expensive operation that scales in the
        # dimension of the input
        for idx in perm:
            mean, log_scale = self.arn(torch.stack(x, dim=-1))
            inverse_scale = torch.exp(-self.tanh(log_scale[..., idx]))
            mean = mean[..., idx]
            x[idx] = (y[..., idx] - mean) * inverse_scale

        x = torch.stack(x, dim=-1)
        self._cached_log_scale = log_scale
        return x

    def log_abs_det_jacobian(self, x, y):
        """
        Calculates the elementwise determinant of the log Jacobian
        """
        x_old, y_old = self._cached_x_y
        if x is not x_old or y is not y_old:
            # This call to the parent class Transform will update the cache
            # as well as calling self._call and recalculating y and log_detJ
            self(x)

        if self._cached_log_scale is not None:
            log_scale = self._cached_log_scale
        else:
            _, log_scale = self.arn(x)
            log_scale = self.tanh(log_scale)
        return log_scale.sum(-1)


class MAF_pyro(nn.ModuleList):
    """ A container for a pyro-based MAF.
    Mainly needs to be implemented for sampling to work
    """

    def __init__(self, num_aux_inputs, num_cond_inputs=None,
                 device=torch.device("cpu"), *args, **kwargs):

        self.num_aux_inputs = num_aux_inputs
        self.num_cond_inputs = num_cond_inputs
        self.device = device

        super().__init__(*args, **kwargs)

    def get_dist(self):

        base_distribution = dist.Normal(
            torch.zeros(self.num_aux_inputs, device=self.device),
            torch.ones(self.num_aux_inputs, device=self.device),
            validate_args=False)

        if self.num_cond_inputs is None:
            distribution = dist.TransformedDistribution(
                base_distribution, self)
        else:
            distribution = dist.ConditionalTransformedDistribution(
                base_distribution, self)

        return distribution

    def sample(self, num_samples=None, cond_inputs=None):

        if cond_inputs is not None:
            device = next(self.parameters()).device
            cond_inputs = cond_inputs.to(device)

            if num_samples > cond_inputs.shape[0]:
                raise ValueError("Number of samples must be at least as large "
                                 "as the number of conditional inputs.")

        current_dist = self.get_dist()

        if self.num_cond_inputs is None:
            sample = current_dist.sample(torch.Size([num_samples, ]))
        else:
            sample = current_dist.condition(
                cond_inputs
                ).sample(torch.Size([num_samples, ]))

        sample = sample[:num_samples]

        return sample

    def log_probs(self, inputs, cond_inputs=None):

        current_dist = self.get_dist()

        if self.num_cond_inputs is None:
            log_prob = current_dist.log_prob(inputs)
        else:
            log_prob = current_dist.condition(cond_inputs).log_prob(inputs)

        return log_prob
