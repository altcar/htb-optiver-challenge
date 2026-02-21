
import torch
import pickle
import numpy as np
from transformers import DistilBertTokenizerFast, DistilBertForSequenceClassification

class StockPredictor:
    def __init__(self, model_path='model-export/model'):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # Load model
        self.model = DistilBertForSequenceClassification.from_pretrained(model_path)
        self.tokenizer = DistilBertTokenizerFast.from_pretrained(model_path)
        self.model.to(self.device)
        self.model.eval()
        
        # Load normalization
        with open('model-export/normalization_params.pkl', 'rb') as f:
            params = pickle.load(f)
            self.means = np.array([params['means'][s] for s in params['stocks']])
            self.stds = np.array([params['stds'][s] for s in params['stocks']])
            self.stocks = params['stocks']
    
    def predict(self, text):
        """Predict stock returns from text"""
        # Tokenize
        inputs = self.tokenizer(
            text, 
            return_tensors='pt', 
            truncation=True, 
            max_length=256, 
            padding='max_length'
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        
        # Predict
        with torch.no_grad():
            outputs = self.model(**inputs)
            preds = outputs.logits.cpu().numpy()
        
        # Denormalize
        preds_denorm = (preds * self.stds + self.means)  # To %
        
        return {self.stocks[i]: float(preds_denorm[0, i]) 
                for i in range(len(self.stocks))}

predictor = StockPredictor()

feeds = ['@BankDaily: Cost reduction measures at ING and Santander improve operational leverage amid headwinds#FinanceWatch #MarketUpdate', '@TechWatch: Nvidia and Cisco report weaker demand in European server segments. #TechNews #Markets', '@NetworkUpdates: Cisco reports accelerated subscription revenue growth supporting long-term stability. #TechNews #MarketUpdate', '@CorporateNews: Facebook parent Meta announces upgraded VR platform; market shows no reaction. #TechNews #MarketUpdate', '@TechRatings: Broker cuts NVDA to Sell following downward revision of quarterly shipments. #AnalystRatings #TechNews', '@MacroReport: Warning signs emerge as major economies exhibit sharp GDP contractions raising recession concerns. #GlobalEconomy #Markets', '@TechUpdate: Software and services segments lead revenue gains at NVIDIA and Cisco in recent quarter. #TechNews #EarningsReport', '@EntertainmentEvents: Comic-Con convention attracts global fandom; no financial implications. #Entertainment #Culture']

for f in feeds:
    print(f)
    print(predictor.predict(f))
    print('')