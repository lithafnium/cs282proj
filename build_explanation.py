import os
import re
import json
import torch
import argparse

import torch.nn.functional as F
from transformers import BertForSequenceClassification, AutoModelForSequenceClassification, AutoTokenizer, AutoModel, DataCollatorWithPadding, DistilBertForSequenceClassification

from torch.utils.data import (
    Dataset,
    DataLoader,
)

import shap
import lime
from lime.lime_text import LimeTextExplainer

from load_glue import *
from train_and_eval import Trainer

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def predict_func(model, tokenizer):
    def predict(x):
        inputs = tokenizer(
            x.tolist(),
            return_tensors="pt",
            truncation=True,
            padding=True,
        ).to(device)

        if model.base_model_prefix != "bert":
            inputs.pop("token_type_ids")
        outputs = model(**inputs)
        logits = outputs["logits"]
        logits = logits[:, 1]
        return logits
        
    return predict
        

def run_shap(model, tokenizer, dataset, args):
    shap_results = {}  # {idx: {'sentence': ..., 'tokens': ..., 'attributions': ..., 'base_value': ...}}
    predict_ = predict_func(model, tokenizer)
    explainer = shap.Explainer(predict_, tokenizer)
    texts, labels = dataset["sentence"], dataset["label"]
    
    bsize = 1
    cur_start = 0
    while cur_start < len(texts):
        texts_ = texts[cur_start:cur_start + bsize]
        print(texts_)
        labels_ = labels[cur_start:cur_start + bsize]
        shap_results_i = explainer(texts_)
        for j in range(bsize):
            shap_results[cur_start+j] = {'sentence': texts_[j],
                                 'tokens': shap_results_i.data[j].tolist(),
                                 'attributions': shap_results_i.values[j].tolist(),
                                 'label': labels_[j],
                                 'base_value': shap_results_i.base_values[j]}
        # move to the next batch of texts
        cur_start += bsize

    return shap_results
    
    
def run_lime(model, tokenizer, dataset, args):
    label_names = [0, 1]
    explainer = LimeTextExplainer(class_names=label_names)
    
    def predictor(texts):
        inputs = tokenizer(texts, return_tensors="pt", truncation=True, padding=True).to(device)
        if not args.is_teacher:
            inputs.pop("token_type_ids")
        outputs = model(**inputs)
        predictions = F.softmax(outputs.logits).cpu().detach().numpy()
        return predictions
        
    lime_results = {}
    for i, t in enumerate(dataset):
        str_to_predict = t["sentence"]
        exp_ = explainer.explain_instance(str_to_predict, predictor, num_features=20, num_samples=500).as_list()
        lime_results[i] = {'sentence': str_to_predict,
                    'tokens': [tp[0] for tp in exp_],
                    'attributions': [tp[1] for tp in exp_],
                    'label': t["label"]}
    
    return lime_results
        


def main():
    parser = argparse.ArgumentParser(description="Build explanation on validation set for models trained on GLUE datasets")
    parser.add_argument("--model_path", type=str, help="Path to model to be evaluated")
    parser.add_argument("--is_teacher", action="store_true", help="Whether the loaded model is a teacher model")
    parser.add_argument("--task", type=str, default="sst2")
    parser.add_argument("--exp_type", type=str, choices=["lime","shap","all"], default="shap", help="Choose one or both of LIME and SHAP to run")
    parser.add_argument("--debug", action="store_true", help="Use validation subset and untrained model for debugging with faster speed")
    
    args = parser.parse_args()
    try:
        match = re.search(r"_([a-zA-Z0-9]+)\.pt", args.model_path)
        args.task = match.group(1) if match else args.task
    except TypeError as e:
        print(f"invalid model path: {e}")
    assert args.task in GLUE_CONFIGS
    
    if args.debug:
        model = AutoModelForSequenceClassification.from_pretrained("distilbert-base-uncased", num_labels=2)
        print(f"model loaded - debug mode")
        model_type = "distilbert_untrained"
    else:
        try:
            match_teacher = re.search(r"teacher_(.+)_", args.model_path)
            match_student = re.search(r"student_(.+)_", args.model_path)
            if match_teacher:
                model_type = match_teacher.group(1)
                args.is_teacher = True
            else:
                model_type = match_student.group(1)
                args.is_teacher = False
            num_labels = 3 if args.task.startswith("mnli") else 1 if args.task=="stsb" else 2
            model = AutoModelForSequenceClassification.from_pretrained(model_type, num_labels=num_labels)
            model.load_state_dict(torch.load(args.model_path))
            print(f"model loaded")
        except FileNotFoundError as e1:
            print(f"error: {e1}")
        except RuntimeError as e2:
            print(f"error: {e2}")
        except TypeError as e3:
            print(f"error: {e3}")
    
    model.to(device)
    print("loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
    print(f"loading validation data for task {args.task}")
    _, val_dataset, val_raw_dataset = train_and_eval_split(tokenizer, args.task)

    data_collator = DataCollatorWithPadding(tokenizer=tokenizer)
    val_dataset.set_format(type="torch", columns=["input_ids", "token_type_ids", "attention_mask", "labels"])
    val_dataset = val_dataset.remove_columns(["token_type_ids"])
    
    if args.debug:
        val_dataset = torch.utils.data.Subset(val_dataset, range(4))
        val_raw_dataset = val_raw_dataset[:4]
    print(f"Validation Data Size: {len(val_dataset)}")
    val_dataloader = DataLoader(val_dataset, batch_size=4, collate_fn=data_collator)
    
    if not os.path.exists(f"explanation_results"):
        os.makedirs(f"explanation_results")
    if not os.path.exists(f"explanation_results/{model_type}"):
        os.makedirs(f"explanation_results/{model_type}")
    

    if args.exp_type == "all" or args.exp_type == "shap":
        shap_results = run_shap(model, tokenizer, val_raw_dataset, args)
        with open(f'explanation_results/{model_type}/{model_type}_{args.task}_shap.json', 'w') as file1:
            json.dump(shap_results, file1)
    if args.exp_type == "all" or args.exp_type == "lime":
        if args.debug:
            val_raw_dataset = [{key: value[i] for key, value in val_raw_dataset.items()} for i in range(len(val_raw_dataset["sentence"]))]
        lime_results = run_lime(model, tokenizer, val_raw_dataset, args)
        with open(f'explanation_results/{model_type}/{model_type}_{args.task}_lime.json', 'w') as file2:
            json.dump(lime_results, file2)
    
    
        
if __name__ == "__main__":
    # command line for debugging: python build_explanation.py --debug --exp_type all
    # otherwise: python build_explanation.py --model_path <model path>
    main()
    



