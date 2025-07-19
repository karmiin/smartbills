# Smart Bills

Sistema intelligente per la gestione delle bollette con AI e analytics avanzati.

## Funzionalità

- **Upload automatico**: Carica bollette PDF in Azure Blob Storage
- **Estrazione AI**: Analisi automatica dei documenti con Azure Document Intelligence
- **Analytics avanzati**: Dashboard interattiva con statistiche e grafici
- **Forecasting intelligente**: Previsioni future con algoritmi avanzati (regressione lineare + EMA + stagionalità)
- **Autenticazione sicura**: Integrazione con Azure AD B2C
- **Database NoSQL**: Storage strutturato in Azure Cosmos DB

## Tecnologie

- **Backend**: Flask, Python
- **Frontend**: Bootstrap, Chart.js, JavaScript
- **Cloud**: Azure (Blob Storage, Document Intelligence, Cosmos DB, AD B2C)
- **Analytics**: NumPy, algoritmi di machine learning locali

## Setup

1. Clona il repository
2. Installa le dipendenze: `pip install -r requirements.txt`
3. Configura Azure nel file `.env`
4. Esegui l'applicazione: `python app.py`

Smart Bills - Gestione intelligente delle bollette con il potere dell'AI.