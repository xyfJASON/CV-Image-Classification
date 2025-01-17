import os
import tqdm
import argparse
from omegaconf import OmegaConf

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from metrics import Accuracy
from tools import build_model, build_optimizer, build_scheduler
from utils.data import load_data
from utils.logger import get_logger
from utils.tracker import StatusTracker
from utils.misc import get_time_str, check_freq, set_seed
from utils.experiment import create_exp_dir, find_resume_checkpoint


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=str, required=True, help='Path to config file')
    parser.add_argument('-e', '--exp_dir', type=str, help='Path to the experiment directory. Default to be ./runs/exp-{current time}/')
    parser.add_argument('-r', '--resume', type=str, help='Resume from a checkpoint. Could be a path or `best` or `latest`')
    parser.add_argument('-cd', '--cover_dir', action='store_true', default=False, help='Cover the experiment directory if it exists')
    return parser


def main():
    # PARSE ARGS AND CONFIGS
    args, unknown_args = get_parser().parse_known_args()
    args.time_str = get_time_str()
    if args.exp_dir is None:
        args.exp_dir = os.path.join('runs', f'exp-{args.time_str}')
    unknown_args = [(a[2:] if a.startswith('--') else a) for a in unknown_args]
    unknown_args = [f'{k}={v}' for k, v in zip(unknown_args[::2], unknown_args[1::2])]
    conf = OmegaConf.load(args.config)
    conf = OmegaConf.merge(conf, OmegaConf.from_dotlist(unknown_args))

    # SET DEVICE
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Using device: {device}', flush=True)

    # CREATE EXPERIMENT DIRECTORY
    exp_dir = args.exp_dir
    create_exp_dir(
        exp_dir=exp_dir, conf_yaml=OmegaConf.to_yaml(conf), subdirs=['ckpt'],
        time_str=args.time_str, exist_ok=args.resume is not None, cover_dir=args.cover_dir,
    )

    # INITIALIZE LOGGER
    logger = get_logger(
        log_file=os.path.join(exp_dir, f'output-{args.time_str}.log'),
        use_tqdm_handler=True, is_main_process=True,
    )

    # INITIALIZE STATUS TRACKER
    status_tracker = StatusTracker(
        logger=logger, print_freq=conf.train.print_freq,
        tensorboard_dir=os.path.join(exp_dir, 'tensorboard'),
        is_main_process=True,
    )

    # SET SEED
    set_seed(conf.seed)
    logger.info('=' * 19 + ' System Info ' + '=' * 18)
    logger.info(f'Experiment directory: {exp_dir}')

    # BUILD DATASET & DATALOADER
    train_set = load_data(conf.data, split='train')
    valid_set = load_data(conf.data, split='valid')
    train_loader = DataLoader(train_set, batch_size=conf.train.batch_size, shuffle=True, drop_last=True, **conf.dataloader)
    valid_loader = DataLoader(valid_set, batch_size=conf.train.batch_size, shuffle=False, drop_last=False, **conf.dataloader)
    logger.info('=' * 19 + ' Data Info ' + '=' * 20)
    logger.info(f'Size of training set: {len(train_set)}')
    logger.info(f'Size of validation set: {len(valid_set)}')
    logger.info(f'Batch size: {conf.train.batch_size}')

    # BUILD MODEL, OPTIMIZER AND SCHEDULER
    model = build_model(conf)
    optimizer = build_optimizer(model.parameters(), conf)
    scheduler = build_scheduler(optimizer, conf)
    model.to(device)
    logger.info('=' * 19 + ' Model Info ' + '=' * 19)
    logger.info(f'Number of parameters of model: {sum(p.numel() for p in model.parameters()):,}')
    logger.info('=' * 50)

    # RESUME TRAINING
    step, best_acc = 0, 0.
    if args.resume is not None:
        resume_path = find_resume_checkpoint(exp_dir, args.resume)
        logger.info(f'Resume from {resume_path}')
        # load model
        ckpt = torch.load(os.path.join(resume_path, 'model.pt'), map_location='cpu', weights_only=True)
        model.load_state_dict(ckpt['model'])
        logger.info(f'Successfully load model from {resume_path}')
        # load training states (optimizer, scheduler, step, best_acc)
        ckpt = torch.load(os.path.join(resume_path, 'training_states.pt'), map_location='cpu', weights_only=True)
        optimizer.load_state_dict(ckpt['optimizer'])
        scheduler.load_state_dict(ckpt['scheduler'])
        step = ckpt['step'] + 1
        best_acc = ckpt['best_acc']
        logger.info(f'Successfully load optimizer from {resume_path}')
        logger.info(f'Successfully load scheduler from {resume_path}')
        logger.info(f'Restart training at step {step}')
        logger.info(f'Best accuracy so far: {best_acc}')
        del ckpt

    # DEFINE LOSSES AND METRICS
    cross_entropy = nn.CrossEntropyLoss().to(device)
    accuracy_fn = Accuracy(topk=(1, 5), reduction='none')

    # TRAINING FUNCTIONS
    def save_ckpt(save_path: str):
        os.makedirs(save_path, exist_ok=True)
        # save model
        torch.save(dict(
            model=model.state_dict(),
        ), os.path.join(save_path, 'model.pt'))
        # save training states (optimizer, scheduler, step, best_acc)
        torch.save(dict(
            optimizer=optimizer.state_dict(),
            scheduler=scheduler.state_dict(),
            step=step,
            best_acc=best_acc,
        ), os.path.join(save_path, 'training_states.pt'))

    def train_step(batch):
        x = batch[0].float().to(device)
        y = batch[1].long().to(device)
        logits = model(x)
        loss = cross_entropy(logits, y)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()
        return dict(loss=loss.item(), lr=optimizer.param_groups[0]['lr'])

    @torch.no_grad()
    def evaluate(dataloader):
        acc1_list, acc5_list = [], []
        for x, y in tqdm.tqdm(dataloader, desc='Evaluating', leave=False):
            x, y = x.float().to(device), y.long().to(device)
            logits = model(x)
            acc1, acc5 = accuracy_fn(logits, y)
            acc1_list.append(acc1)
            acc5_list.append(acc5)
        acc1_list = torch.cat(acc1_list).cpu()
        acc5_list = torch.cat(acc5_list).cpu()
        return {
            'acc@1': acc1_list.mean().item(),
            'acc@5': acc5_list.mean().item(),
        }

    # START TRAINING
    logger.info('Start training...')
    while step < conf.train.n_steps:
        for _batch in tqdm.tqdm(train_loader, desc='Epoch', leave=False):
            if step >= conf.train.n_steps:
                break
            # train a step
            model.train()
            train_status = train_step(_batch)
            status_tracker.track_status('Train', train_status, step)
            # validate
            model.eval()
            # evaluate
            if check_freq(conf.train.eval_freq, step):
                # evaluate on training set
                eval_status_train = evaluate(train_loader)
                eval_status_train = {f'{k}(train_set)': v for k, v in eval_status_train.items()}
                status_tracker.track_status('Eval', eval_status_train, step)
                # evaluate on validation set
                eval_status_valid = evaluate(valid_loader)
                eval_status_valid = {f'{k}(valid_set)': v for k, v in eval_status_valid.items()}
                status_tracker.track_status('Eval', eval_status_valid, step)
                # save the best model
                if eval_status_valid['acc@1(valid_set)'] > best_acc:
                    best_acc = eval_status_valid['acc@1(valid_set)']
                    save_ckpt(os.path.join(exp_dir, 'ckpt', 'best'))
            # save checkpoint
            if check_freq(conf.train.save_freq, step):
                save_ckpt(os.path.join(exp_dir, 'ckpt', f'step{step:0>6d}'))
            step += 1
    # save the last checkpoint if not saved
    if not check_freq(conf.train.save_freq, step - 1):
        save_ckpt(os.path.join(exp_dir, 'ckpt', f'step{step-1:0>6d}'))
    logger.info(f'Best valid accuracy: {best_acc:.4f}')

    # END OF TRAINING
    status_tracker.close()
    logger.info('End of training')


if __name__ == '__main__':
    main()
