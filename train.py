"""Model training."""

import datetime
import os
import random
import time
import pytz
import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold
import sklearn
import torch
from torch.utils.data import DataLoader
import torch.nn as nn

import metrics
from cross_validate import make_folds
from dataset import Melanoma
from model import CustomEfficientNet
from config import YAMLConfig
import transforms
from meters import AverageLossMeter, AccuracyMeter
from utils import seed_all, seed_worker
from loss import LabelSmoothingLoss


class Trainer:
    """A class to perform model training."""
    def __init__(self, model, config, early_stopping=None):
        """Construct a Trainer instance."""
        self.model = model
        self.config = config
        self.early_stopping = early_stopping
        self.epoch = 0
        self.save_path = config.paths["save_path"]
        if not os.path.exists(self.save_path):
            os.makedirs(self.save_path)

        # Uncomment this if needed to use different val loss #
        # self.criterion = LabelSmoothingLoss(**config.criterion_params[config.criterion]).to(self.config.device)
        # self.criterion_val = getattr(torch.nn, config.criterion_val)(**config.criterion_params[config.criterion_val])
        self.criterion = getattr(
            torch.nn,
            config.criterion)(**config.criterion_params[config.criterion])
        self.optimizer = getattr(torch.optim, config.optimizer)(
            self.model.parameters(),
            **config.optimizer_params[config.optimizer])
        self.scheduler = getattr(torch.optim.lr_scheduler, config.scheduler)(
            optimizer=self.optimizer,
            **config.scheduler_params[config.scheduler])

        self.selected_metrics = [
            metrics.construct_metric(metric, self.config)
            for metric in self.config.metrics
        ]
        self.validation_metrics = metrics.ValidationMetrics(
            self, self.selected_metrics, self.config)

        # The current-known best values for selected ComparableMetric metrics
        self.best_metrics = {}
        # The current-known values for selected savable metrics
        self.saved_metrics = {}
        # https://stackoverflow.com/questions/1398674/display-the-time-in-a-different-time-zone
        self.date = datetime.datetime.now(
            pytz.timezone("Asia/Singapore")).strftime("%Y-%m-%d")

        self.log("Trainer prepared. We are using {} device with {} worker(s).".
                 format(self.config.device, self.config.num_workers))

    def fit(self, train_loader, val_loader, fold: int):
        """Fit the model on the given fold."""
        self.log("Training on Fold {} and using {}".format(
            fold, self.config.effnet))

        for _epoch in range(self.config.n_epochs):
            # Getting the learning rate after each epoch!
            lr = self.optimizer.param_groups[0]["lr"]

            timestamp = datetime.datetime.now(
                pytz.timezone("Asia/Singapore")).strftime("%Y-%m-%d %H-%M-%S")
            # printing the lr and the timestamp after each epoch.
            self.log("\n{}\nLR: {}".format(timestamp, lr))

            # start time of training on the training set
            train_start_time = time.time()

            # train one epoch on the training set
            avg_train_loss, avg_train_acc_score = self.train_one_epoch(
                train_loader)
            # end time of training on the training set
            train_end_time = time.time()

            # formatting time to make it nicer
            train_elapsed_time = time.strftime(
                "%H:%M:%S", time.gmtime(train_end_time - train_start_time))
            self.log(
                "[RESULT]: Train. Epoch {} | Avg Train Summary Loss: {:.6f} | "
                "Train Accuracy: {:6f} | Time Elapsed: {}".format(
                    self.epoch + 1, avg_train_loss, avg_train_acc_score,
                    train_elapsed_time))

            val_start_time = time.time()
            val_results = self.valid_one_epoch(val_loader)
            val_end_time = time.time()
            val_elapsed_time = time.strftime(
                "%H:%M:%S", time.gmtime(val_end_time - val_start_time))

            # Save the current value of all savable metrics
            for metric in self.selected_metrics:
                if not isinstance(metric, metrics.SavableMetric):
                    continue

                savable_name = metric.get_save_name(
                    val_results[metric.__class__.__name__])

                # If savable_name is reported as None, we don't save the
                # metric value.
                if savable_name is None:
                    continue

                self.saved_metrics[savable_name] = val_results[
                    metric.__class__.__name__]

            val_reported_results = [
                metric.report(val_results[metric.__class__.__name__])
                for metric in self.selected_metrics
                if isinstance(metric, metrics.ReportableMetric)
            ]

            result_str = " | ".join([
                "Validation. Epoch: {}".format(self.epoch + 1),
                *val_reported_results,
                "Time Elapsed: {}".format(val_elapsed_time)
            ])

            self.log("[RESULT]: {}".format(result_str))

            if self.early_stopping is not None:
                best_score, early_stop = self.early_stopping.should_stop(
                    curr_epoch_score=val_results[self.config.monitored_metric])
                """
                Be careful of self.best_loss here, when our monitered_metrics is val_roc_auc, then we should instead write
                self.best_auc = best_score. After which, if early_stop flag becomes True, then we break out of the training loop.
                """

                self.best_metrics[self.config.monitored_metric] = best_score
                self.save("{}_best_{}_fold_{}.pt".format(
                    self.config.effnet, self.config.monitored_metric, fold))
                if early_stop:
                    break

            # Compute the new best value for all selected ComparableMetric
            # metrics. If we find a new best value for the selected monitored
            # metric, save the model.
            for metric in self.selected_metrics:
                if not isinstance(metric, metrics.ComparableMetric):
                    continue

                old_value = self.best_metrics.get(metric.__class__.__name__,
                                                  None)

                if old_value is None:
                    self.best_metrics[metric.__class__.__name__] = old_value

                    if (metric.__class__.__name__ ==
                            self.config.monitored_metric):
                        self.save(
                            os.path.join(
                                self.save_path,
                                "{}_{}_best_{}_fold_{}.pt".format(
                                    self.date, self.config.effnet,
                                    self.config.monitored_metric, fold)))

                    continue

                new_value = val_results[metric.__class__.__name]

                if metric.compare(old_value, new_value):
                    self.best_metrics[metric.__class__.__name__] = new_value

                    if (metric.__class__.__name__ ==
                            self.config.monitored_metric):
                        self.save(
                            os.path.join(
                                self.save_path,
                                "{}_{}_best_{}_fold_{}.pt".format(
                                    self.date, self.config.effnet,
                                    self.config.monitored_metric, fold)))
            """
            Usually, we should call scheduler.step() after the end of each epoch. In particular, we need to take note that
            ReduceLROnPlateau needs to step(monitered_metrics) because of the mode argument.
            """
            if self.config.val_step_scheduler:
                if isinstance(self.scheduler,
                              torch.optim.lr_scheduler.ReduceLROnPlateau):
                    self.scheduler.step(
                        val_results[self.config.monitored_metric])
                else:
                    self.scheduler.step()

            # end of training, epoch + 1 so that self.epoch can be updated.
            self.epoch += 1

        # this is where we end the epoch training for the current
        # fold/model, therefore we can call the final "best weight
        # saved" by this exact name that we saved earlier on.
        curr_fold_best_checkpoint = self.load(
            os.path.join(
                self.save_path,
                "{}_{}_best_{}_fold_{}.pt".format(self.date,
                                                  self.config.effnet,
                                                  self.config.monitored_metric,
                                                  fold)))
        # return the checkpoint for further usage.
        return curr_fold_best_checkpoint

    def train_one_epoch(self, train_loader):
        """Train one epoch of the model."""
        # set to train mode
        self.model.train()

        # log metrics
        summary_loss = AverageLossMeter()
        accuracy_scores = AccuracyMeter()

        # timer
        start_time = time.time()
        """ Looping through train loader for one epoch, steps is the number of times to go through each epoch"""
        for step, (_image_ids, images, labels) in enumerate(train_loader):
            """1. images, labels: Moving image and label tensors to self.device,
            meaning to say we move them to cuda if there is GPU, and
            cpu if otherwise.

            2. With train_loader having a batch size of 4, this means
            each loop will only load 4 images.

            3. print(images.shape, labels.shape, images.dtype,
            labels.dtype, images.type(), labels.type()) -->
            torch.Size([4, 3, 256, 256]), torch.Size([4]),
            torch.float32, torch.int64, torch.cuda.FloatTensor,
            torch.cuda.LongTensor

            Rightfully so, because now images is a
            4d-tensor-array(tensor) containing 4 batches of images, in
            the form [channels, img_size, img_size]=[3, 256, 256]
            Similar logic goes to labels, just that it is a
            1d-tensor-array.

            Reference:
            https://pytorch.org/docs/stable/tensor_attributes.html;
            https://pytorch.org/docs/stable/tensors.html"""

            images = images.to(self.config.device)
            labels = labels.to(self.config.device)
            """1. batch_size:
            There is a catch here, one should never use batch_size =
            config.batch_size in this loop. The logic is if we have 30
            images, with a batch size of 4, then there will be 8
            loops, but the last loop will only have 2 images passing
            through, WHICH IS NOT THE SAME AS config.batch_size. This
            batch_size is reserved for calculating loss/accuracy/roc
            etc and must match the number of images/labels in each
            loop.
            """

            batch_size = images.shape[0]
            """1. logits:
            logits is equivalent to model.forward(images) which
            outputs raw computation. In other words, when the model
            reaches the final layer, right before the sigmoid/softmax
            activation, those values are called logits. As a reminder,
            one can envision the last layer's logits to be of the form
            z = w^Tx+b where w represents a weight matrix, and x is
            the matrix before the final layer with b being the bias
            (like linear/logistic regression). A simple mental model
            should be given in future.
            2.
            print(logits.shape, logits.dtype, logits.type()) -->
            torch.Size([4, 2]), torch.float32, torch.cuda.FloatTensor
            The shape has now changed, and is worth noting that our
            logits/output is a tensor-array of 4 by 2, which is akin
            to a 2-d array with 4 rows and 2 columns - each row has 2
            values since our num_classes = 2. These logits are
            important in the next 2-3 steps.
            3.
            print(logits.requires_grad) --> bool: True This is because
            we are in the training phase, and logits are needed for
            Backpropagation, and Backpropagation needs to store
            gradients.
            4.
            print(logits) --> tensor([[ 0.0802,  0.0347],
                                      [ 0.2640, -0.0701],
                                      [-0.0087,  0.0502],
                                      [ 0.0496,  0.0059]], device='cuda:0',
                                     grad_fn=<AddmmBackward>)
            """
            logits = self.model(images)
            """1. loss: criterion in this case is torch.nn.CrossEntropyLoss(),
            which is a loss function where we pass in raw logits and
            labels.
            2. summary_loss.update(loss.item(), batch_size): Refer to
               AverageLossMeter() to understand what it does, the idea
               is to calculate the total loss after every epoch.
            3. self.optimizer.zero_grad(): clear gradient weights,
            unique in PyTorch, call it before loss.backward()!
            4. loss.backward(): computes all gradients wrt all
            weights/parameters in the neural network. Backpropagation
            in action!
            5. self.optimizer.step(): In our case, this will do - this
               is where we update all the weights of the parameters
               after backpropagation, using our favourite optimizer,
               be it gradient descent, or any optimization function.
            """

            loss = self.criterion(input=logits, target=labels)
            summary_loss.update(loss.item(), batch_size)
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            """ 1. y_true: in this current loop, the DataLoader has 4 images and
            its corresponding labels stored. We want to know what are
            the labels/class.  To convert the labels to numpy, we need
            to use .cpu().numpy().
               print(y_true, y_true.shape) --> [0 0 0 0], (4,)
            2. softmax_preds: pass our LOGITS (raw predictions) to the
               SoftMax function. The SoftMax function will turn all
               current logits into a probability, assigned to each
               class.
               print(softmax_preds, softmax_preds.shape) -->
                    [[0.51865923 0.4813407 ]
                    [0.5439908  0.45600918]
                    [0.44908398 0.550916  ]
                    [0.51905555 0.4809445 ]]   (4,2)
               Notice that each array inside sums up to 1. So in a way
               the first batch of 4 images in the first loop churns
               out predictions, claiming that the first image has a
               51.86 percent chance of being class 0 -> beneign, and
               notice that the third image has a 55.09 percent of
               being class 1 -> maligant.
            3. y_preds: We perform a numpy operation .argmax(1) on the
                        softmax_preds array.  I have updated
                        softmax_preds.argmax(1) to
                        np.argmax(a=softmax_preds, axis = 1) for
                        clarity.  Both are the same. Take note we are
                        performing this operation on a 2d-array, and
                        on axis = 1.  One can refer to
                        https://stackoverflow.com/questions/17079279/how-is-axis-indexed-in-numpys-array
                        for more insight.
               print(y_preds, y_preds.shape) --> [0 0 1 0] , (4,)
               This conversion is basically converting all the
               probabilities to the real labels. This step is
               necessary as we are going to feed both y_true and
               y_preds into our AccuracyMeter() - in which we can get
               an accuracy score.  i.e. ground truth of the first 4
               images: [0,0,0,0] prediction on the first 4 images:
               [0,0,1,0] accuracy score: 75%
            4. accuracy_scores.update(y_true, y_preds,
            batch_size=batch_size): See point 3 and AccuracyMeter().
            """

            y_true = labels.cpu().numpy()
            softmax_preds = torch.nn.Softmax(dim=1)(
                input=logits).to("cpu").detach().numpy()
            y_preds = np.argmax(a=softmax_preds, axis=1)

            accuracy_scores.update(y_true, y_preds, batch_size=batch_size)

            # not too sure yet KIV
            if self.config.train_step_scheduler:
                self.scheduler.step()

            # measure elapsed time
            end_time = time.time()

            if self.config.verbose:
                if (step % self.config.verbose_step) == 0:
                    print(
                        f"Train Steps {step}/{len(train_loader)}, "
                        f"summary_loss: {summary_loss.avg:.3f}, "
                        f"acc: {accuracy_scores.avg:.3f} "
                        f"time: {(end_time - start_time):.3f}",
                        end="\r",
                    )

        return summary_loss.avg, accuracy_scores.avg

    def valid_one_epoch(self, val_loader):
        """Validate one training epoch."""
        # set to eval mode
        self.model.eval()

        return self.validation_metrics.compute_metrics(val_loader)

    def save_model(self, path):
        """Save the trained model."""
        self.model.eval()
        torch.save(self.model.state_dict(), path)

    def save(self, path):
        """Save the weight for the best evaluation loss (and monitored metrics) with corresponding OOF predictions.
        OOF predictions for each fold is merely the best score for that fold."""
        self.model.eval()

        best_metrics = {
            "best_{}".format(best_metric): value
            for (best_metric, value) in self.best_metrics.items()
        }

        torch.save(
            {
                "model_state_dict": self.model.state_dict(),
                "optimizer_state_dict": self.optimizer.state_dict(),
                "scheduler_state_dict": self.scheduler.state_dict(),
                "epoch": self.epoch,
                **best_metrics,
                **self.saved_metrics,
            },
            path,
        )

    def load(self, path):
        """Load a model checkpoint from the given path."""
        checkpoint = torch.load(path)
        return checkpoint

    def log(self, message):
        """Log a message."""
        if self.config.verbose:
            print(message)
        with open(self.config.paths["log_path"], "a+") as logger:
            logger.write(f"{message}\n")


def train_on_fold(df_folds: pd.DataFrame, config, fold: int):
    """Train the model on the given fold."""
    model = CustomEfficientNet(config=config, pretrained=True)
    model.to(config.device)

    augmentations_class = getattr(transforms, config.augmentations_class)

    transforms_train = augmentations_class.from_config(
        config.augmentations_train[config.augmentations_class])
    transforms_val = augmentations_class.from_config(
        config.augmentations_val[config.augmentations_class])

    train_df = df_folds[df_folds["fold"] != fold].reset_index(drop=True)
    val_df = df_folds[df_folds["fold"] == fold].reset_index(drop=True)

    train_set = Melanoma(
        train_df,
        config,
        transforms=transforms_train,
        transform_norm=True,
        meta_features=None,
    )
    train_loader = DataLoader(train_set,
                              batch_size=config.batch_size,
                              shuffle=True,
                              num_workers=config.num_workers,
                              worker_init_fn=seed_worker)

    val_set = Melanoma(val_df,
                       config,
                       transforms=transforms_val,
                       transform_norm=True,
                       meta_features=None)
    val_loader = DataLoader(val_set,
                            batch_size=config.batch_size,
                            shuffle=False,
                            num_workers=config.num_workers,
                            worker_init_fn=seed_worker)

    melanoma_detector = Trainer(model=model, config=config)

    curr_fold_best_checkpoint = melanoma_detector.fit(train_loader, val_loader,
                                                      fold)

    val_df[[str(c) for c in range(config.num_classes)
            ]] = curr_fold_best_checkpoint["oof_preds"]
    val_df["preds"] = curr_fold_best_checkpoint["oof_preds"].argmax(1)

    return val_df


def train_loop(df_folds: pd.DataFrame,
               config,
               fold_num: int = None,
               train_one_fold=False):
    """Perform the training loop on all folds."""
    # here The CV score is the average of the validation fold metric.
    cv_score_list = []
    oof_df = pd.DataFrame()
    if train_one_fold:
        _oof_df = train_on_fold(df_folds=df_folds,
                                config=config,
                                fold=fold_num)
        curr_fold_best_score = get_result(config, _oof_df)
        print("Fold {} OOF Score is {}".format(fold_num, curr_fold_best_score))
    else:
        """The below for loop code guarantees fold starts from 1 and not 0. https://stackoverflow.com/questions/33282444/pythonic-way-to-iterate-through-a-range-starting-at-1"""
        for fold in (number + 1 for number in range(config.num_folds)):
            _oof_df = train_on_fold(df_folds=df_folds,
                                    config=config,
                                    fold=fold)
            oof_df = pd.concat([oof_df, _oof_df])
            curr_fold_best_score = get_roc(config, _oof_df)
            cv_score_list.append(curr_fold_best_score)
            print("\n\n\nOOF Score for Fold {}: {}\n\n\n".format(
                fold, curr_fold_best_score))

    print("CV score", np.mean(cv_score_list))
    print("Variance", np.var(cv_score_list))
    print("Five Folds OOF", get_roc(config, oof_df))
    oof_df.to_csv("oof.csv")


if __name__ == "__main__":
    yaml_config = YAMLConfig("./config.yaml")
    seed_all(seed=yaml_config.seed)
    train_csv = pd.read_csv(yaml_config.paths["csv_path"])
    folds = make_folds(train_csv, yaml_config)

    train_single_fold = train_on_fold(folds, yaml_config, fold=1)
