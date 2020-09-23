'''MIT License

Copyright (C) 2020 Prokofiev Kirill
 
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom
the Software is furnished to do so, subject to the following conditions:
 
The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.
 
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.'''

import json
import logging
import os
import os.path as osp
import sys
from importlib import import_module

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from attrdict import AttrDict as adict
from torch.autograd import Variable
from torch.utils.data import DataLoader, Dataset

from datasets import LCFAD, CasiaSurfDataset, CelebASpoofDataset, MultiDataset
from losses import (AMSoftmaxLoss, AngleSimpleLinear, SoftTripleLinear,
                    SoftTripleLoss)
from models import Dropout, mobilenetv2, mobilenetv3_large, mobilenetv3_small


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def check_file_exist(filename, msg_tmpl='file "{}" does not exist'):
    if not osp.isfile(filename):
        raise FileNotFoundError(msg_tmpl.format(filename))

def read_py_config(filename):
    filename = osp.abspath(osp.expanduser(filename))
    check_file_exist(filename)
    assert filename.endswith('.py')
    module_name = osp.basename(filename)[:-3]
    if '.' in module_name:
        raise ValueError('Dots are not allowed in config file path.')
    config_dir = osp.dirname(filename)
    sys.path.insert(0, config_dir)
    mod = import_module(module_name)
    sys.path.pop(0)
    cfg_dict = adict({
        name: value
        for name, value in mod.__dict__.items()
        if not name.startswith('__')
    })

    return cfg_dict

def save_checkpoint(state, filename="my_model.pth.tar"):
    print('==> saving checkpoint')
    torch.save(state, filename)

def load_checkpoint(checkpoint_path, net, map_location, optimizer=None, load_optimizer=False, strict=True):
    ''' load a checkpoint of the given model. If model is using for training with imagenet weights provided by
        this project, then delete some wights due to mismatching architectures'''
    print("\n==> Loading checkpoint")
    checkpoint = torch.load(checkpoint_path, map_location=map_location)
    if 'state_dict' in checkpoint:
        missing_keys, unexpected_keys = net.load_state_dict(checkpoint['state_dict'], strict=strict)
    else:
        missing_keys, unexpected_keys = (', '.join(i) for i in net.load_state_dict(checkpoint, strict=strict))
    if missing_keys or unexpected_keys:
        logging.warning(f'NEXT KEYS HAVE NOT BEEN LOADED:\n\nmissing keys: {missing_keys}\n\nunexpected keys: {unexpected_keys}\n')
        print('proceed traning ...')
    if load_optimizer:
        optimizer.load_state_dict(checkpoint['optimizer'])
    if 'epoch' in checkpoint:
        return checkpoint['epoch']

def precision(output, target, s=None):
    """Compute the precision"""
    if s:
        output = output*s
    if type(output) == tuple:
        output = output[0].data
    accuracy = (output.argmax(dim=1) == target).float().mean().item()
    return accuracy*100

def mixup_target(input, target, config, device, num_classes=2):
    # compute mix-up augmentation
    input, target_a, target_b, lam = mixup_data(input, target, config.aug.alpha, config.aug.beta, device)
    return input, target_a, target_b, lam

def mixup_data(x, y, alpha=1.0, beta=1.0, device='cuda:0'):
    '''Returns mixed inputs, pairs of targets, and lambda'''
    if alpha > 0:
        lam = np.random.beta(alpha, beta)
    else:
        lam = 1

    batch_size = x.size()[0]
    index = torch.randperm(batch_size).to(device)

    mixed_x = lam * x + (1 - lam) * x[index, :]
    y_a, y_b = y, y[index]
    return mixed_x, y_a, y_b, lam

def cutmix(input, target, config, device='cuda:0', num_classes=2):
    r = np.random.rand(1)
    if (config.aug.beta > 0) and (config.aug.alpha > 0) and (r < config.aug.cutmix_prob):
        # generate mixed sample
        lam = np.random.beta(config.aug.alpha > 0, config.aug.beta > 0)
        rand_index = torch.randperm(input.size()[0]).to(device)
        bbx1, bby1, bbx2, bby2 = rand_bbox(input.size(), lam)
        input[:, :, bbx1:bbx2, bby1:bby2] = input[rand_index, :, bbx1:bbx2, bby1:bby2]
        # adjust lambda to exactly match pixel ratio
        lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (input.size()[-1] * input.size()[-2]))
        target_a = target
        target_b = target[rand_index]
        return input, target_a, target_b, lam

    return input, target, target, 0


def rand_bbox(size, lam):
    W = size[2]
    H = size[3]
    cut_rat = np.sqrt(1. - lam)
    cut_w = np.int(W * cut_rat)
    cut_h = np.int(H * cut_rat)

    # uniform
    cx = np.random.randint(W)
    cy = np.random.randint(H)

    bbx1 = np.clip(cx - cut_w // 2, 0, W)
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = np.clip(cx + cut_w // 2, 0, W)
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return bbx1, bby1, bbx2, bby2

def freeze_layers(model, open_layers):
    for name, module in model.named_children():
        if name in open_layers:
            module.train()
            for p in module.parameters():
                p.requires_grad = True
        else:
            module.eval()
            for p in module.parameters():
                p.requires_grad = False

def make_dataset(config: dict, train_transform: object = None, val_transform: object = None, mode='train', eval_protocol='standart'):
    ''' make train, val or test datasets '''
    celeba_root = config.datasets.Celeba_root
    lccfasd_root = config.datasets.LCCFASD_root
    casia_root = config.datasets.Casia_root

    if mode == 'train': 
        if config.dataset == 'LCC_FASD':
            train =  LCFAD(root_dir=lccfasd_root, protocol='train', transform=train_transform)
            val = LCFAD(root_dir=lccfasd_root, protocol='val', transform=val_transform)
        elif config.dataset == 'celeba-spoof':
            train =  CelebASpoofDataset(root_folder=celeba_root, test_mode=False, 
                                        transform=train_transform, multi_learning=config.multi_task_learning)
            val = CelebASpoofDataset(root_folder=celeba_root, test_mode=True, 
                                     transform=val_transform, multi_learning=config.multi_task_learning)
        elif config.dataset == 'Casia':
            train = CasiaSurfDataset(protocol=1, dir=casia_root, mode='train', transform=train_transform)
            val = CasiaSurfDataset(protocol=1, dir=casia_root, mode='dev', transform=val_transform)
        elif config.dataset == 'multi_dataset':
            train = MultiDataset(lccfasd_root, celeba_root, train=True, transform=train_transform, 
                                 LCFASD_train_protocol='combine_all', LCFASD_val_protocol='val_test')
            val = MultiDataset(lccfasd_root, celeba_root, train=False, transform=val_transform, 
                               LCFASD_train_protocol='combine_all', LCFASD_val_protocol='val_test')
        return train, val

    if mode == 'eval':
        if config.test_dataset.type == 'LCC_FASD' and config.dataset == 'celeba-spoof':
            test = LCFAD(root_dir=lccfasd_root, protocol='combine_all', transform=val_transform)
        elif config.test_dataset.type == 'LCC_FASD':
            assert config.dataset != 'celeba-spoof'
            test = LCFAD(root_dir=lccfasd_root, protocol='test', transform=val_transform)
        elif config.test_dataset.type == 'Casia':
            test = CasiaSurfDataset(protocol=1, dir=casia_root, mode='test', transform=val_transform)
        elif config.test_dataset.type == 'celeba-spoof':
            test = CelebASpoofDataset(root_folder=celeba_root, test_mode=True, transform=val_transform, multi_learning=config.multi_task_learning)
        return test

def make_loader(train, val, config, sampler=None):
    ''' make data loader from given train and val dataset
    train, val -> train loader, val loader'''
    if sampler:
        shuffle = False
    else:
        shuffle = True
    train_loader = DataLoader(dataset=train, batch_size=config.data.batch_size,
                                                    shuffle=shuffle, pin_memory=config.data.pin_memory,
                                                    num_workers=config.data.data_loader_workers, sampler=sampler)

    val_loader = DataLoader(dataset=val, batch_size=config.data.batch_size,
                                                shuffle=True, pin_memory=config.data.pin_memory,
                                                num_workers=config.data.data_loader_workers)
    return train_loader, val_loader

def build_model(config, device, strict=True):
    ''' build model and change layers depends on loss type'''
    parameters = dict(prob_dropout=config.dropout.prob_dropout,
                    type_dropout=config.dropout.type,
                    mu=config.dropout.mu,
                    sigma=config.dropout.sigma,
                    embeding_dim=config.model.embeding_dim,
                    prob_dropout_linear = config.dropout.classifier,
                    theta=config.conv_cd.theta,
                    multi_heads = config.multi_task_learning,
                    to_forward=config.model.to_forward)

    if config.model.model_type == 'Mobilenet2':
        model = mobilenetv2(**parameters)

        if config.model.pretrained:
            checkpoint_path = config.model.imagenet_weights
            load_checkpoint(checkpoint_path, model, strict=strict, map_location=device)
        
        if (config.loss.loss_type == 'amsoftmax') and (config.loss.amsoftmax.margin_type != 'cross_entropy'):
            model.spoofer = AngleSimpleLinear(config.model.embeding_dim, 2)

        elif config.loss.loss_type == 'soft_triple':
            model.spoofer = SoftTripleLinear(config.model.embeding_dim, 2, num_proxies=config.loss.soft_triple.K)
    else:
        assert config.model.model_type == 'Mobilenet3'
        if config.model.model_size == 'large':
            model = mobilenetv3_large(**parameters)

            if config.model.pretrained:
                checkpoint_path = config.model.imagenet_weights
                load_checkpoint(checkpoint_path, model, strict=strict, map_location=device)
        else:
            assert config.model.model_size == 'small'
            model = mobilenetv3_small(**parameters)

            if config.model.pretrained:
                checkpoint_path = config.model.imagenet_weights
                load_checkpoint(checkpoint_path, model, strict=strict, map_location=device)

        if (config.loss.loss_type == 'amsoftmax') and (config.loss.amsoftmax.margin_type != 'cross_entropy'):
            model.spoofer[3] = AngleSimpleLinear(config.model.embeding_dim, 2)

        elif config.loss.loss_type == 'soft_triple':
            model.spoofer[3] = SoftTripleLinear(config.model.embeding_dim, 2, num_proxies=config.loss.soft_triple.K)

    return model

def build_criterion(config, device, task='main'):
    if task == 'main':
        if config.loss.loss_type == 'amsoftmax':
            criterion = AMSoftmaxLoss(**config.loss.amsoftmax, device=device)
        elif config.loss.loss_type == 'soft_triple':
            criterion = SoftTripleLoss(**config.loss.soft_triple)
    else:
        assert task == 'rest'
        criterion = AMSoftmaxLoss(margin_type='cross_entropy', 
                                  label_smooth=config.loss.amsoftmax.label_smooth,
                                  smoothing=config.loss.amsoftmax.smoothing,
                                  gamma=config.loss.amsoftmax.gamma,
                                  device=device)
    return criterion

class Transform():
    """ class to make diferent transform depends on the label """
    def __init__(self, train_spoof=None, train_real=None, val = None):
        self.train_spoof = train_spoof
        self.train_real = train_real
        self.val_transform = val
        if not all((self.train_spoof, self.train_real)):
            self.train = self.train_spoof or self.train_real
            self.transforms_quantity = 1
        else:
            self.transforms_quantity = 2
    def __call__(self, label, img):
        if self.val_transform:
            return self.val_transform(image=img)
        if self.transforms_quantity == 1:
            return self.train(image=img)
        if label:
            return self.train_spoof(image=img)
        else:
            assert label == 0
            return self.train_real(image=img)

def make_weights(config):
    '''load weights for imbalance dataset to list'''
    if config.dataset != 'celeba-spoof':
        raise NotImplementedError
    with open(os.path.join(config.data.data_root, 'metas/intra_test/items_train.json') , 'r') as f:
        dataset = json.load(f)
    n = len(dataset)
    weights = [0 for i in range(n)]
    keys = list(map(int, list(dataset.keys())))
    keys.sort()
    assert len(keys) == n
    for key in keys:
        label = int(dataset[str(key)]['labels'][43])
        if label:
            weights[int(key)] = 0.1
        else:
            assert label == 0
            weights[int(key)] = 0.2

    assert len(weights) == n
    return weights


