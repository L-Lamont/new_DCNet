import os
import logging
import argparse
import gc
import cv2

from fastai.vision.all import *
from fastai.distributed import *
import torch.distributed as dist
from PIL import ImageFile, Image, ImageDraw
ImageFile.LOAD_TRUNCATED_IMAGES = True
import torch
import numpy as np
import time

from utils import setup, max_px, extract_predictions, classes2name, cleanup
from data import get_dataloader, load_labels
from model import get_model, FocalLoss


def parse_args():
    """Parse command line arguments"""

    parser = argparse.ArgumentParser('train.py')
    add_arg = parser.add_argument
    add_arg(
            '--output', 
            help='Set the output directory'
    )
    add_arg(
            '--data', 
            help='Specify the directory with input'
    )
    add_arg(
            '--batch-size', 
            type=int, 
            help='Specify the batch size', 
            default=2
    )
    add_arg(
            '--num-epochs', 
            type=int, 
            help='Specify the number of epochs', 
            default=60
    )
    add_arg(
            '--seed', 
            type=int, 
            help='Set the seed for random numbers', 
            default=42
    )
    add_arg(
            '--dist',
            type=int,
            help='Set the distribution type',
            default=0
    )

    return parser.parse_args()


def config_logging():
    """Configures logging"""

    log_format = '%(asctime)s %(levelname)s %(message)s'
    log_level = logging.INFO
    logging.basicConfig(level=log_level, format=log_format)


def check_args(args):
    """Checks validity of command line arguments"""
    
    if args.output is None:                                                     
        raise Exception('args.output is not set')                               
    if not os.path.isdir(args.output):                                          
        raise Exception('args.output: {} is not a directory'.                   
                format(args.output)                                             
        )                                                                       
                                                                                
    if args.data is None:                                                       
        raise Exception('args.data is not set')                                 
    if not os.path.exists(args.data):                                           
        raise Exception('args.data: {} does not exist'.                         
                format(args.data)                                               
        )                                                                       
                                                                                
    if args.batch_size < 0:                                                     
        raise Exception('args.batch_size = {} it must be positive'.             
                format(args.batch_size)                                         
        )                                                                       
                                                                                
    if args.num_epochs < 0:                                                     
        raise Exception('args.num_epochs = {} it must be positive'.             
                format(args.num_epochs)                                         
        )                                                                       
                                                                                
    if args.seed < 0:                                                           
        raise Exception('args.seed = {} it must be positive'.                   
                format(args.seed)                                               
        ) 

def main():
    """Main function"""

    # Setup
    args = parse_args()
    check_args(args)
    config_logging()

    dist = setup(args)

    logging.info('sanity check dist.local_rank: {}'.format(dist.local_rank))
    device = torch.device('cuda:{}'.format(str(dist.local_rank)))
    os.environ['CUDA_VISIBLE_DEVICES'] = str(dist.local_rank)
    torch.cuda.set_device(device)

    if dist.rank == 0:
        logging.info('Finished setup')

    # Setup Variables
    Eosinophil = (1, 0.6,  0)  # orange                                        
    Lymphocyte = (0,   1,  0)  # green                                         
    MacroMono = (1,   0,  1)  # red                                           
    Neutrophil = (0,   1,  1)  # cyan                                          
                                                                                    
    TILE_SZ = 1024                                                                  
                                                                                    
    class_map = {                                                                   
        # LBName: (idx, display, color, radius)  # average radius scenario          
        'Eosinophil': (0, 'Eosinophil', Eosinophil, 61/TILE_SZ),                    
        'Lymphocyte': (1, 'Lymphocyte', Lymphocyte, 30/TILE_SZ),                    
        'Macrophage': (2, 'Macro+Mono', MacroMono,  76/TILE_SZ),                    
        'Monocyte':   (2, 'Macro+Mono', MacroMono,  76/TILE_SZ),                    
        'Neutrophil': (3, 'Neutrophil', Neutrophil, 52/TILE_SZ)                     
    }                                                                               
                                                                                    
    classes = sorted(set(class_map.values()))                                       
                                                                                    
    if dist.rank == 0:
        logging.info('TILE_SZ:\t{}'.format(TILE_SZ))                                    
        logging.info('Classes:\n{}'.format(classes))

    # Get dataloader
    dls = get_dataloader(args, dist, 'Model/img2msk.pkl', holdout=False)
    dls.c = len(classes)

    # Get model
    learn = get_model(dls, dist)

    # Collect garbage and clear GPU cache
    gc.collect()
    torch.cuda.empty_cache()

    # Distribute model and move it to the GPU
    learn.model = learn.model.to(device)

    # Starting training
    with learn.distrib_ctx(sync_bn=False, cuda_id=int(dist.local_rank)):
        learn.freeze()
        loss_func = FocalLoss(F.mse_loss)
        learn.fit(1, 1e-4, wd=1e-5)

        if dist.rank == 0:
            logging.info('Finished 1 epoch learn.fit')

        learn.loss_func = FocalLoss()
        learn.fit(4, 2e-4, wd=1e-5)

        if dist.rank == 0:
            logging.info('Finished 4 epoch learn.fit')

        learn.unfreeze()
        learn.fit_one_cycle(args.num_epochs, 5e-4, wd=1e-5)

        if dist.rank == 0:
            logging.info('Finished {} epoch fit_one_cycle'.format(args.num_epochs))

    learn.load(os.path.join(args.data, 'Model/3g_latest_mean_IVTs'))

    min_area = 4

    # Evaluation on holdout dataset
    ho_dls = get_dataloader(args, dist, 'Model/holdout.pkl', holdout=True)
    holdout_lbls = load_labels(args, dist)

    start = time.time()
    
    inps,preds,lbls = learn.get_preds(dl=ho_dls, with_input=True)
    preds = preds.detach().cpu().numpy()
    lbls = lbls.detach().cpu().numpy()
    ho_out = ho_dls.decode_batch((inps, lbls), max_n=len(holdout_lbls))  

    ho_results = []   
    for idx in range(len(holdout_lbls[:])):
        inp, _ = ho_out[idx]
        pred, lbl = preds[idx], lbls[idx]
        pred_points, lbl_points = extract_predictions(lbl, pred, score_thresh=0.1, min_area=4)
        
        target = classes2name(lbl_points, classes)
        prediction = classes2name(pred_points, classes)
        
        fname = holdout_lbls[idx]['External ID']
        # for creating dataframes for the holdout dataset
        row = target + prediction
        row.append(fname)
        ho_results.append(row)

         
    end = time.time()

    cleanup()

    logging.info('time to predict {} tiles: {} seconds'.format((idx+1), (end-start)))

if __name__ == '__main__':
    main()
