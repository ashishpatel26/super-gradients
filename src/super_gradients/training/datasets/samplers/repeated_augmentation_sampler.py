import math
import torch
from torch.utils.data import Sampler
import torch.distributed as dist


class RepeatAugSampler(Sampler):
    """
    Sampler that restricts data loading to a subset of the dataset for distributed,
    with repeated augmentation.
    It ensures that different each augmented version of a sample will be visible to a
    different process (GPU). Heavily based on torch.utils.data.DistributedSampler
    This sampler was taken from https://github.com/facebookresearch/deit/blob/0c4b8f60/samplers.py
    Copyright (c) 2015-present, Facebook, Inc.

    Below code is modified from:
     https://github.com/rwightman/pytorch-image-models/blame/master/timm/data/distributed_sampler.py

    Note this sampler is currently supported only for DDP training.

    Arguments:
        dataset (torch.utils.data.Dataset): dataset to sample from.
        shuffle (bool): whether to shuffle the dataset indices (default=True).
        num_repeats (int): amount of repetitions for each example.
    """

    def __init__(
            self,
            dataset: torch.utils.data.Dataset,
            shuffle: bool = True,
            num_repeats: int = 3
    ):
        if not dist.is_available():
            raise RuntimeError("Requires distributed package to be available")

        num_replicas = dist.get_world_size()
        rank = dist.get_rank()
        self.dataset = dataset
        self.num_replicas = num_replicas
        self.rank = rank
        self.shuffle = shuffle
        self.num_repeats = num_repeats
        self.epoch = 0
        self.num_samples = int(math.ceil(len(self.dataset) * num_repeats / self.num_replicas))
        self.total_size = self.num_samples * self.num_replicas
        self.num_selected_samples = int(math.ceil(len(self.dataset) / num_replicas))

    def __iter__(self):
        # deterministically shuffle based on epoch
        g = torch.Generator()
        g.manual_seed(self.epoch)
        if self.shuffle:
            indices = torch.randperm(len(self.dataset), generator=g)
        else:
            indices = torch.arange(start=0, end=len(self.dataset))

        # produce repeats e.g. [0, 0, 0, 1, 1, 1, 2, 2, 2....]
        if isinstance(self.num_repeats, float) and not self.num_repeats.is_integer():
            # resample for repeats w/ non-integer ratio
            repeat_size = math.ceil(self.num_repeats * len(self.dataset))
            indices = indices[torch.tensor([int(i // self.num_repeats) for i in range(repeat_size)])]
        else:
            indices = torch.repeat_interleave(indices, repeats=int(self.num_repeats), dim=0)
        indices = indices.tolist()  # leaving as tensor thrashes dataloader memory
        # add extra samples to make it evenly divisible
        padding_size = self.total_size - len(indices)
        if padding_size > 0:
            indices += indices[:padding_size]
        assert len(indices) == self.total_size

        # subsample per rank
        indices = indices[self.rank:self.total_size:self.num_replicas]
        assert len(indices) == self.num_samples

        # return up to num selected samples
        return iter(indices[:self.num_selected_samples])

    def __len__(self):
        return self.num_selected_samples

    def set_epoch(self, epoch):
        self.epoch = epoch
