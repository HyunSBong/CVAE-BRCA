import math
import numpy as np
import pandas as pd
import os
import argparse
import datetime
from datetime import datetime
import pickle
import sys

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, SubsetRandomSampler
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.metrics import f1_score
from sklearn.model_selection import KFold


from moBRCAnet_gene_pytorch_model import moBRCAnet, SoftmaxClassifier

# import wandb

def load_data(train_x, test_x, train_y, test_y, n_gene):
    X_train = pd.read_csv(train_x, delimiter=",", dtype=np.float32)
    X_test = pd.read_csv(test_x, delimiter=",", dtype=np.float32)
    Y_train = pd.read_csv(train_y, delimiter=",", dtype=np.float32)
    Y_test = pd.read_csv(test_y, delimiter=",", dtype=np.float32)

    X_gene_train = X_train.values
    X_gene_test = X_test.values

    n_classes = 5
    
    dataset = {'train_set': (X_train, Y_train),
               'test_set': (X_test, Y_test),
               'gene_set': (X_gene_train, X_gene_test),
               'n_classes': n_classes
              }
    return dataset

def main(args, dataset):
    # wandb.init(project="moBRCAnet gene level pytorch 0626", reinit=True)
    # wandb.config.update(args)

    ### GPU 
    os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"]= str(args.gpu_id)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print('Device:', device)
    print('Current cuda device:', torch.cuda.current_device())
    print('Count of using GPUs:', torch.cuda.device_count())
    
    # Train/Test dataset
    (X_train, Y_train) = dataset['train_set']
    (X_test, Y_test) = dataset['test_set']
    (X_gene_train, X_gene_test) = dataset['gene_set']
    n_classes = dataset['n_classes']
    
    X = pd.concat([X_train, X_test], axis=0).values
    Y = pd.concat([Y_train, Y_test], axis=0).values
    
    # Scaler
    scaler = StandardScaler().fit(X)
    X = scaler.transform(X)
    
    train_labels = Y
    n_classes = 5
    
    ### DataLoader
    train_label = torch.as_tensor(train_labels)
    train = torch.tensor(X.astype(np.float32))
    train_tensor = TensorDataset(train, train_label)

    ### Hyperparameter
    softmax_hidden = 200 # 200
    dropout_rate = 0.2
    ensemble_model_num = 1
    n_gene = args.n_gene
    n_sm_out = n_classes # 5
    n_embedding = args.n_embedding # 128
    epochs = args.epochs
    batch_size = args.batch_size
    learning_rate = args.learning_rate
    l2scale = args.l2scale
    fc_output_size = 64
    
    # K-fold CV
    k = args.kfold
    kfold = KFold(n_splits=k, shuffle=True)
    
    cv_acc = []
    cv_f1 = []
    print(f'data => {args.save_path}')
    
    for fold, (train_idx, val_idx) in enumerate(kfold.split(train_tensor)):
        print(f'----- fold : {fold} -----')
        train_subsampler = SubsetRandomSampler(train_idx)
        val_subsampler = SubsetRandomSampler(val_idx)
        
        train_loader = DataLoader(dataset=train_tensor, batch_size=args.batch_size, sampler=train_subsampler)
        val_loader = DataLoader(dataset=train_tensor, batch_size=args.batch_size, sampler=val_subsampler)
        
        ### Model
        moBrca = moBRCAnet(
            output_size = fc_output_size,
            n_features = n_gene,
            n_embedding = n_embedding,
            dropout_rate = dropout_rate,
            num_features = n_gene
        ).to(device)

        if args.multi_omics == False:
            softmax_module = SoftmaxClassifier(
                n_embedding = 64,
                softmax_output = softmax_hidden,
                n_classes = n_classes,
                dropout_rate = dropout_rate
            ).to(device)

        if args.multi_omics == True:
            softmax_module = SoftmaxClassifier(
                n_embedding = 128,
                softmax_output = softmax_hidden,
                n_classes = n_classes,
                dropout_rate = dropout_rate
            ).to(device)
        
        ### loss, optimizer
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(moBrca.parameters(), lr=args.learning_rate)

        max_accr = 0
        max_f1 = 0
        stop_point = 0

        for epoch in range(args.epochs):

            sum_loss = 0
            sum_acc = 0

            for batch_idx, (x, y) in enumerate(train_loader):
                optimizer.zero_grad()

                x, y = x.to(device), y.to(device)
                if x.is_cuda != True:
                    x = x.cuda()
                rep_gene, _ = moBrca(x)

                if args.multi_omics == False:
                    outputs = softmax_module(rep_gene)

                if args.multi_omics == False:
                    outputs = softmax_module(rep_gene)
                    
                loss = criterion(outputs, y)
                loss = torch.mean(loss)

                pred = torch.argmax(outputs, dim=1)
                label = torch.argmax(y, dim=1)
                correct_pred = torch.eq(pred, label)
                accuracy = torch.mean(correct_pred.float())

                sum_loss += loss
                sum_acc += accuracy

                loss.backward(retain_graph=True)
                optimizer.step()

            avg_loss = sum_loss / len(train_loader)
            avg_acc = sum_acc / len(train_loader)

            # print("Epoch {:02d}/{:02d} Loss {:9.4f}, Accuracy {:9.4f},".format(
            #     epoch+1, args.epochs, avg_loss, avg_acc))

            if stop_point > 20:
                break

            if max_accr > float(avg_acc):
                stop_point += 1

            if max_accr < float(avg_acc):
                max_accr = avg_acc
                stop_point = 0

        cur_acc = 0
        cur_f1 = 0
        cur_pred = 0
        cur_label = 0
        cur_attn_gene = 0

        with torch.no_grad():
            sum_loss = 0
            sum_acc = 0
            sum_f1 = 0

            for batch_idx, (x, y) in enumerate(val_loader):
                x, y = x.to(device), y.to(device)
                if x.is_cuda != True:
                    x = x.cuda()

                rep_gene, cur_attn_gene = moBrca(x)
                outputs = softmax_module(rep_gene)

                loss =  criterion(outputs, y)
                cur_pred = torch.argmax(outputs, dim=1)
                cur_label = torch.argmax(y, dim=1)
                correct_pred = torch.eq(cur_pred, cur_label)
                accuracy = torch.mean(correct_pred.float())

                pred_cpu = pred.clone().cpu().numpy().flatten()
                label_cpu = label.clone().cpu().numpy().flatten()

                f1 = f1_score(label_cpu, pred_cpu, average='weighted')

                sum_loss += loss
                sum_acc += accuracy
                sum_f1 += f1

            avg_loss = sum_loss / len(val_loader)
            cur_acc = sum_acc / len(val_loader)
            cur_f1 = sum_f1 / len(val_loader)

            # print("===> cur_accr:%.6f," % cur_acc, "weighted F1:%.6f," % cur_f1)

        if len(cv_acc) != 0 and max(cv_acc) < cur_acc.detach().cpu().numpy():
            np.savetxt(f"./results{args.save_path}" + "prediction_cv.csv", cur_pred.cpu().numpy(), fmt="%.0f", delimiter=",")
            np.savetxt(f"./results{args.save_path}" + "label_cv.csv", cur_label.cpu().numpy(), fmt="%.0f", delimiter=",")
            np.savetxt(f"./results{args.save_path}" + "attn_score_gene_cv.csv", cur_attn_gene.cpu().numpy(), fmt="%f", delimiter=",")

            date_val = datetime.today().strftime("%Y%m%d%H%M")    
            file = f'./results{args.save_path}mobrca_gene_{date_val}_accuracy{cur_acc}_cv.pkl'
            data = {'moBrca': moBrca,
                    'softmax_module': softmax_module,
                    }
            with open(file, 'wb') as files:
                pickle.dump(data, files)
                
            print('saved!')

        print(f"ACCURACY => {str(cur_acc.detach().cpu().numpy())}")
        cv_acc.append(cur_acc.detach().cpu().numpy())
        cv_f1.append(cur_f1)
        
    print(f"ACCURACY average for {k}-fold: ==> {sum(cv_acc)/len(cv_acc)}")
    cv_acc = pd.DataFrame(cv_acc, columns=["accuracy"])
    cv_f1 = pd.DataFrame(cv_f1, columns=["f1"])
    df = pd.concat([cv_acc, cv_f1], axis=1)
    df.to_csv(f"./results{args.save_path}" + "result_cv.csv", index=False)

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu_id",type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=8000)
    parser.add_argument("--batch_size", type=int, default=136)
    parser.add_argument("--learning_rate", type=float, default=1e-2)
    parser.add_argument("--l2scale",type=float, default=0.00001)
    parser.add_argument("--n_embedding",type=int, default=128)
    parser.add_argument("--fc_output",type=int, default=64)
    parser.add_argument("--n_gene",type=int, default=352)
    parser.add_argument("--multi_omics", type=lambda s: s. lower() in ['true', '1'], default=False)
    parser.add_argument("--train_x",type=str)
    parser.add_argument("--test_x",type=str)
    parser.add_argument("--train_y",type=str)
    parser.add_argument("--test_y",type=str)
    parser.add_argument("--save_path",type=str, default='/')
    parser.add_argument("--kfold",type=int, default=10)
    
    args = parser.parse_args()
    
    dataset = load_data(args.train_x, args.test_x, args.train_y, args.test_y, args.n_gene)
    main(args, dataset)
     
        

        
