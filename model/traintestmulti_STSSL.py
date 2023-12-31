import sys
import os
import shutil
import numpy as np
import pandas as pd
import time
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
from torchsummary import summary
import pickle
import argparse
import logging
from utils import StandardScaler, DataLoader, masked_mae_loss, masked_mape_loss, masked_mse_loss, masked_rmse_loss, load_pickle
from STSSL import STSSL

def print_model(model):
    param_count = 0
    logger.info('Trainable parameter list:')
    for name, param in model.named_parameters():
        if param.requires_grad:
            print(name, param.shape, param.numel())
            param_count += param.numel()
    logger.info(f'In total: {param_count} trainable parameters.')
    return

def get_model():  
    model = STSSL(args.d_model, args.input_length, args.num_nodes, args.nmb_prototype, args.dropout, args.input_dim, args.output_dim, args.shm_temp,
                  args.percent, args.batch_size, device).to(device)
    return model

def load_graph(adj_file, device='cpu'):
    '''Loading graph in form of edge index.'''
    graph = load_pickle(adj_file)[2]
    graph = torch.tensor(graph, device=device, dtype=torch.float)
    return graph

def prepare_x_y(x, y):
    """
    :param x: shape (batch_size, seq_len, num_sensor, input_dim)
    :param y: shape (batch_size, horizon, num_sensor, input_dim)
    :return1: x shape (seq_len, batch_size, num_sensor, input_dim)
              y shape (horizon, batch_size, num_sensor, input_dim)
    :return2: x: shape (seq_len, batch_size, num_sensor * input_dim)
              y: shape (horizon, batch_size, num_sensor * output_dim)
    """
    x0 = x[..., :args.input_dim]
    y0 = y[..., :args.output_dim]
    y1 = y[..., args.output_dim:]
    x0 = torch.from_numpy(x0).float()
    y0 = torch.from_numpy(y0).float()
    y1 = torch.from_numpy(y1).float()
    return x0.to(device), y0.to(device), y1.to(device) # x, y, y_cov

def evaluate(model, mode, multi_bool=False):
    with torch.no_grad():
        model = model.eval()
        graph = load_graph(graph_path, device=device)
        data_iter =  data[f'{mode}_loader'].get_iterator()
        ys_true, ys_pred = [], []
        for x, y in data_iter:
            x, y, _ = prepare_x_y(x, y)
            if multi_bool == False:
                y = y[:, 0:1, :, :]
                rep1, rep2 = model(x, graph)
                pred = model.predict(rep1, rep2)
                y_pred = scaler.inverse_transform(pred)
                y_true = scaler.inverse_transform(y)
                ys_true.append(y_true)
                ys_pred.append(y_pred)
            else:
                x_multi, y_pred_multi = [x],[]
                for i in range(args.horizon):
                    x_ = torch.cat(x_multi, axis=1)[:, i:, :, :]
                    rep1, rep2 = model(x_, graph)
                    pred = model.predict(rep1, rep2)
                    x_multi.append(pred)
                    y_pred_multi.append(pred)                                                            
                y_pred_multi = torch.cat(y_pred_multi, axis=1)                
                y_pred = scaler.inverse_transform(y_pred_multi)
                y_true = scaler.inverse_transform(y)
                ys_pred.append(y_pred)
                ys_true.append(y_true)
        
        y_size = data[f'y_{mode}'].shape[0]
        ys_true, ys_pred = torch.cat(ys_true, dim=0)[:y_size], torch.cat(ys_pred, dim=0)[:y_size]
        loss = masked_mae_loss(ys_pred, ys_true)

        if mode == 'test':
            ys_true, ys_pred = ys_true.permute(1, 0, 2, 3), ys_pred.permute(1, 0, 2, 3)
            mae = masked_mae_loss(ys_pred, ys_true).item()
            mape = masked_mape_loss(ys_pred, ys_true).item()
            rmse = masked_rmse_loss(ys_pred, ys_true).item()
            if multi_bool == False:
                logger.info('Horizon Next-setp : mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae, mape, rmse))
            else:
                mae_multi, mape_multi, rmse_multi = [], [], []
                for i in [2, 5, 11]:
                    mae_i = masked_mae_loss(ys_pred[i:i+1], ys_true[i:i+1]).item()
                    mae_multi.append(mae_i)
                    mape_i = masked_mape_loss(ys_pred[i:i+1], ys_true[i:i+1]).item()
                    mape_multi.append(mape_i)
                    rmse_i = masked_rmse_loss(ys_pred[i:i+1], ys_true[i:i+1]).item()
                    rmse_multi.append(rmse_i)
                logger.info('Horizon overall: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae, mape, rmse))
                logger.info('Horizon 15mins: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae_multi[0], mape_multi[0], rmse_multi[0]))
                logger.info('Horizon 30mins: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae_multi[1], mape_multi[1], rmse_multi[1]))
                logger.info('Horizon 60mins: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae_multi[2], mape_multi[2], rmse_multi[2]))    
            ys_true, ys_pred = ys_true.permute(1, 0, 2, 3), ys_pred.permute(1, 0, 2, 3)
        return loss, ys_true, ys_pred

    
def traintest_model():  
    model = get_model()
    print_model(model)
    graph = load_graph(graph_path, device=device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, eps=args.epsilon)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=args.steps, gamma=args.lr_decay_ratio)
    min_val_loss = float('inf')
    wait = 0
    batches_seen = 0
    for epoch_num in range(args.epochs):
        start_time = time.time()
        model = model.train()
        data_iter = data['train_loader'].get_iterator()
        losses = []
        for x, y in data_iter:
            optimizer.zero_grad()
            x, y, _ = prepare_x_y(x, y)                        
            y = y[:, 0:1, :, :]                           
            rep1, rep2 = model(x, graph)
            pred = model.predict(rep1, rep2)
            y_pred = scaler.inverse_transform(pred)
            y_true = scaler.inverse_transform(y)
            stssl_loss = model.loss(rep1, rep2)
            reg_loss = masked_mae_loss(y_pred, y_true)
            loss = reg_loss + stssl_loss
            losses.append(loss.item())
            batches_seen += 1
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm) # gradient clipping - this does it in place
            optimizer.step()
        train_loss = np.mean(losses)
        lr_scheduler.step()
        val_loss, _, _ = evaluate(model, 'val')
        end_time2 = time.time()
        message = 'Epoch [{}/{}] ({}) train_loss: {:.4f}, val_loss: {:.4f}, lr: {:.6f}, {:.1f}s'.format(epoch_num + 1, 
                   args.epochs, batches_seen, train_loss, val_loss, optimizer.param_groups[0]['lr'], (end_time2 - start_time))
        logger.info(message)
        test_loss, _, _ = evaluate(model, 'test')

        if val_loss < min_val_loss:
            wait = 0
            min_val_loss = val_loss
            torch.save(model.state_dict(), modelpt_path)
            # logger.info('Val loss decrease from {:.4f} to {:.4f}, saving model to pt'.format(min_val_loss, val_loss))
        elif val_loss >= min_val_loss:
            wait += 1
            if wait == args.patience:
                logger.info('Early stopping at epoch: %d' % epoch_num)
                break
    
    logger.info('=' * 35 + 'Best model performance' + '=' * 35)
    model = get_model()
    model.load_state_dict(torch.load(modelpt_path))
    test_loss, _, _ = evaluate(model, 'test', multi_bool=True)

#########################################################################################    
parser = argparse.ArgumentParser()
parser.add_argument('--dataset', type=str, choices=['METRLA', 'PEMSBAY'], default='METRLA', help='which dataset to run')
parser.add_argument('--trainval_ratio', type=float, default=0.8, help='the ratio of training and validation data among the total')
parser.add_argument('--val_ratio', type=float, default=0.125, help='the ratio of validation data among the trainval ratio')
parser.add_argument('--num_nodes', type=int, default=207, help='num_nodes')
parser.add_argument('--input_length', type=int, default=12, help='input sequence length')
parser.add_argument('--horizon', type=int, default=12, help='output sequence length')
parser.add_argument('--input_dim', type=int, default=1, help='number of input channel')
parser.add_argument('--output_dim', type=int, default=1, help='number of output channel')
parser.add_argument('--d_model', type=int, default=64, help='number of hidden channel')
parser.add_argument('--dropout', type=float, default=0.2, help='drop rate')
parser.add_argument('--percent', type=float, default=0.1, help='augumentation percentage')
parser.add_argument('--shm_temp', type=float, default=0.5, help='temperature for loss of spatial heterogeneity modeling')
parser.add_argument('--nmb_prototype', type=int, default=6, help='number of cluster')
parser.add_argument('--yita', type=float, default=0.5, help='balance for inflow loss and outflow loss, $yita * inflow + (1 - yita) * outflow$')
parser.add_argument("--loss", type=str, default='mask_mae_loss', help="mask_mae_loss")
parser.add_argument("--epochs", type=int, default=200, help="number of epochs of training")
parser.add_argument("--patience", type=int, default=20, help="patience used for early stop")
parser.add_argument("--batch_size", type=int, default=16, help="size of the batches")
parser.add_argument("--lr", type=float, default=0.01, help="base learning rate")
parser.add_argument("--steps", type=eval, default=[50, 100], help="steps")
parser.add_argument("--lr_decay_ratio", type=float, default=0.1, help="lr_decay_ratio")
parser.add_argument("--epsilon", type=float, default=1e-3, help="optimizer epsilon")
parser.add_argument("--max_grad_norm", type=int, default=5, help="max_grad_norm")
parser.add_argument("--cl_decay_steps", type=int, default=2000, help="cl_decay_steps")
parser.add_argument('gpu', type=int, default=0, help='which gpu to use')
# parser.add_argument('--seed', type=int, default=100, help='random seed.')
args = parser.parse_args()
        
if args.dataset == 'METRLA':
    data_path = f'../{args.dataset}/metr-la.h5'
    graph_path = f'../{args.dataset}/adj_mx_la.pkl'
    args.num_nodes = 207
elif args.dataset == 'PEMSBAY':
    data_path = f'../{args.dataset}/pems-bay.h5'
    graph_path = f'../{args.dataset}/adj_mx_bay.pkl'
    args.num_nodes = 325
else:
    pass # including more datasets in the future    

model_name = 'STSSL'
timestring = time.strftime('%Y%m%d%H%M%S', time.localtime())
path = f'../save/{args.dataset}_{model_name}_{timestring}'
logging_path = f'{path}/{model_name}_{timestring}_logging.txt'
score_path = f'{path}/{model_name}_{timestring}_scores.txt'
epochlog_path = f'{path}/{model_name}_{timestring}_epochlog.txt'
modelpt_path = f'{path}/{model_name}_{timestring}.pt'
if not os.path.exists(path): os.makedirs(path)
shutil.copy2(sys.argv[0], path)
shutil.copy2(f'{model_name}.py', path)
shutil.copy2('utils.py', path)
    
logger = logging.getLogger(__name__)
logger.setLevel(level = logging.INFO)
class MyFormatter(logging.Formatter):
    def format(self, record):
        spliter = ' '
        record.msg = str(record.msg) + spliter + spliter.join(map(str, record.args))
        record.args = tuple() # set empty to args
        return super().format(record)
formatter = MyFormatter()
handler = logging.FileHandler(logging_path, mode='a')
handler.setLevel(logging.INFO)
handler.setFormatter(formatter)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(formatter)
logger.addHandler(handler)
logger.addHandler(console)

logger.info('model', model_name)
logger.info('dataset', args.dataset)
logger.info('trainval_ratio', args.trainval_ratio)
logger.info('val_ratio', args.val_ratio)
logger.info('seq_len', args.input_length)
logger.info('horizon', args.horizon)
logger.info('num_nodes', args.num_nodes)
logger.info('input_dim', args.input_dim)
logger.info('output_dim', args.output_dim)
logger.info('d_model', args.d_model)
logger.info('nmb_prototype', args.nmb_prototype)
logger.info('loss', args.loss)
logger.info('batch_size', args.batch_size)
logger.info('epochs', args.epochs)
logger.info('patience', args.patience)
logger.info('lr', args.lr)
logger.info('epsilon', args.epsilon)
logger.info('steps', args.steps)
logger.info('lr_decay_ratio', args.lr_decay_ratio)
logger.info('cl_decay_steps', args.cl_decay_steps)

cpu_num = 1
os.environ ['OMP_NUM_THREADS'] = str(cpu_num)
os.environ ['OPENBLAS_NUM_THREADS'] = str(cpu_num)
os.environ ['MKL_NUM_THREADS'] = str(cpu_num)
os.environ ['VECLIB_MAXIMUM_THREADS'] = str(cpu_num)
os.environ ['NUMEXPR_NUM_THREADS'] = str(cpu_num)
torch.set_num_threads(cpu_num)
device = torch.device("cuda:{}".format(args.gpu)) if torch.cuda.is_available() else torch.device("cpu")
# Please comment the following three lines for running experiments multiple times.
# np.random.seed(args.seed)
# torch.manual_seed(args.seed)
# if torch.cuda.is_available(): torch.cuda.manual_seed(args.seed)
#####################################################################################################

data = {}
for category in ['train', 'val', 'test']:
    cat_data = np.load(os.path.join(f'../{args.dataset}', category + '.npz'))
    data['x_' + category] = cat_data['x']
    data['y_' + category] = cat_data['y']
scaler = StandardScaler(mean=data['x_train'][..., 0].mean(), std=data['x_train'][..., 0].std())
for category in ['train', 'val', 'test']:
    data['x_' + category][..., 0] = scaler.transform(data['x_' + category][..., 0])
    data['y_' + category][..., 0] = scaler.transform(data['y_' + category][..., 0])
data['train_loader'] = DataLoader(data['x_train'], data['y_train'], args.batch_size, shuffle=True)
data['val_loader'] = DataLoader(data['x_val'], data['y_val'], args.batch_size, shuffle=False)
data['test_loader'] = DataLoader(data['x_test'], data['y_test'], args.batch_size, shuffle=False)

def main():
    logger.info(args.dataset, 'training and testing started', time.ctime())
    logger.info('train xs.shape, ys.shape', data['x_train'].shape, data['y_train'].shape)
    logger.info('val xs.shape, ys.shape', data['x_val'].shape, data['y_val'].shape)
    logger.info('test xs.shape, ys.shape', data['x_test'].shape, data['y_test'].shape)
    traintest_model()
    logger.info(args.dataset, 'training and testing ended', time.ctime())
    
if __name__ == '__main__':
    main()
