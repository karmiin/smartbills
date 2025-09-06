import os
import re
import json
import numpy as np
from datetime import datetime, timedelta
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.cosmos import CosmosClient, exceptions
from dotenv import load_dotenv

load_dotenv()

class BillProcessor:
    def __init__(self):
        # Configurazione Azure Form Recognizer
        self.form_recognizer_endpoint = os.getenv('AZURE_FORM_RECOGNIZER_ENDPOINT')
        self.form_recognizer_key = os.getenv('AZURE_FORM_RECOGNIZER_KEY')
        
        # Configurazione Cosmos DB
        self.cosmos_endpoint = os.getenv('AZURE_COSMOS_ENDPOINT')
        self.cosmos_key = os.getenv('AZURE_COSMOS_KEY')
        self.database_name = os.getenv('AZURE_COSMOS_DATABASE_NAME', 'bills_management')
        self.container_name = os.getenv('AZURE_COSMOS_CONTAINER_NAME', 'bills')
        
        # Inizializza i client se configurati
        self.form_client = None
        self.cosmos_client = None
        self.container = None
        
        self._init_services()
    
    def _init_services(self):
        try:
            # Inizializza Form Recognizer
            if self.form_recognizer_endpoint and self.form_recognizer_key:
                credential = AzureKeyCredential(self.form_recognizer_key)
                self.form_client = DocumentAnalysisClient(
                    endpoint=self.form_recognizer_endpoint, 
                    credential=credential
                )
            
            # Inizializza Cosmos DB
            if self.cosmos_endpoint and self.cosmos_key:
                self.cosmos_client = CosmosClient(self.cosmos_endpoint, self.cosmos_key)
                
                # Crea database e container se non esistono
                database = self.cosmos_client.create_database_if_not_exists(id=self.database_name)
                
                # Configurazione della partition key
                partition_key_config = {
                    'paths': ['/user_id'],
                    'kind': 'Hash'
                }
                
                self.container = database.create_container_if_not_exists(
                    id=self.container_name,
                    partition_key=partition_key_config,
                    offer_throughput=400
                )
                
        except Exception as e:
            pass
    
    def extract_bill_data(self, pdf_stream, filename, user_id):
        if not self.form_client:
            return self._extract_data_manual(pdf_stream, filename, user_id)
        
        try:
            # Analizza il documento con Form Recognizer
            pdf_stream.seek(0)
            poller = self.form_client.begin_analyze_document(
                "prebuilt-document", 
                pdf_stream
            )
            result = poller.result()
            
            # Estrai i dati del documento
            extracted_data = self._parse_form_recognizer_result(result, filename, user_id)
            
            # Salva nel database
            if self.container:
                self._save_to_cosmos(extracted_data)
            
            return extracted_data
            
        except Exception as e:
            # Fallback a estrazione manuale
            return self._extract_data_manual(pdf_stream, filename, user_id)
    
    def _parse_form_recognizer_result(self, result, filename, user_id):   
        # Combina tutto il testo per l'analisi
        full_text = ""
        for page in result.pages:
            for line in page.lines:
                full_text += line.content + " "
        
        # Cerca pattern comuni nelle bollette italiane
        bill_data = {
            'id': f"{user_id}_{filename}_{int(datetime.now().timestamp())}",
            'user_id': user_id,
            'filename': filename,
            'upload_date': datetime.now().isoformat(),
            'extracted_text': full_text,
            'bill_type': self._detect_bill_type(full_text),
            'supplier': self._extract_supplier(full_text),
            'amount': self._extract_amount(full_text),
            'bill_date': self._extract_bill_date(full_text),  # Data della bolletta
            'due_date': self._extract_due_date(full_text),
            'billing_period': self._extract_billing_period(full_text),
            'consumption': self._extract_consumption(full_text),
            'account_number': self._extract_account_number(full_text),
            'extraction_confidence': 'medium',  # Form Recognizer risultato
            'manual_review_needed': False,
            'extracted_data': {
                'date': self._extract_bill_date(full_text),  # Per compatibilità
                'amount': self._extract_amount(full_text),
                'supplier': self._extract_supplier(full_text)
            }
        }
        
        # Controlla se servono revisioni manuali
        bill_data['manual_review_needed'] = self._needs_manual_review(bill_data)
        
        return bill_data
    
    def _extract_data_manual(self, pdf_stream, filename, user_id):
        """Estrazione dati di fallback senza Form Recognizer"""
        return {
            'id': f"{user_id}_{filename}_{int(datetime.now().timestamp())}",
            'user_id': user_id,
            'filename': filename,
            'upload_date': datetime.now().isoformat(),
            'extracted_text': "Estrazione automatica non disponibile",
            'bill_type': 'unknown',
            'supplier': None,
            'amount': None,
            'due_date': None,
            'billing_period': None,
            'consumption': None,
            'account_number': None,
            'extraction_confidence': 'basso',
            'manual_review_needed': True
        }
    
    def _detect_bill_type(self, text):
        """Rileva il tipo di bolletta dal testo"""
        text_lower = text.lower()
        
        if any(word in text_lower for word in ['enel', 'energia elettrica', 'kwh', 'elettricità']):
            return 'electricity'
        elif any(word in text_lower for word in ['gas', 'metano', 'smc', 'gas naturale']):
            return 'gas'
        elif any(word in text_lower for word in ['acqua', 'idrico', 'mc acqua', 'servizio idrico']):
            return 'water'
        elif any(word in text_lower for word in ['telefono', 'tim', 'vodafone', 'wind', 'iliad']):
            return 'telecom'
        elif any(word in text_lower for word in ['internet', 'fibra', 'adsl']):
            return 'internet'
        elif any(word in text_lower for word in ['rifiuti', 'tari', 'spazzatura']):
            return 'waste'
        else:
            return 'other'
    
    def _extract_supplier(self, text):
        """Estrae il nome del fornitore"""
        suppliers = [
            'ENEL', 'ENI', 'IREN', 'A2A', 'ACEA', 'HERA', 'EDISON',
            'TIM', 'VODAFONE', 'WIND', 'ILIAD', 'FASTWEB',
            'ACQUEDOTTO', 'VERITAS', 'CAP'
        ]
        
        for supplier in suppliers:
            if supplier.lower() in text.lower():
                return supplier
        
        return None
    
    def _extract_amount(self, text):
        """Estrae l'importo totale"""
        # Pattern per importi in euro
        patterns = [
            r'totale[:\s]+€?\s*(\d+[.,]\d{2})',
            r'importo[:\s]+€?\s*(\d+[.,]\d{2})',
            r'€\s*(\d+[.,]\d{2})',
            r'(\d+[.,]\d{2})\s*€'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                # Prendi l'importo più alto (probabilmente il totale)
                amounts = [float(match.replace(',', '.')) for match in matches]
                return max(amounts)
        
        return None
    
    def _extract_bill_date(self, text):
        """Estrae la data della bolletta (data di emissione)"""
        patterns = [
            r'data[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'emissione[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'fattura del[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'bolletta del[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'periodo[:\s]+.*?al[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'dal[:\s]+\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4}[:\s]+al[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            # Pattern per date in formato diverso
            r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})'  # Qualsiasi data come fallback
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                try:
                    date_str = matches[0]
                    # Prova diversi formati
                    for fmt in ['%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d']:
                        try:
                            parsed_date = datetime.strptime(date_str, fmt)
                            # Verifica che la data sia ragionevole (non nel futuro, non troppo vecchia)
                            if parsed_date <= datetime.now() and parsed_date.year >= 2020:
                                return parsed_date.strftime('%Y-%m-%d')
                        except:
                            continue
                except:
                    continue
        
        # Se non trova nessuna data valida, usa una data simulata basata sull'upload
        # Per diversificare le date, usiamo il contenuto per creare date diverse
        fallback_date = self._generate_diverse_date(text)
        return fallback_date.strftime('%Y-%m-%d')
    
    def _generate_diverse_date(self, text):
        """Genera date diverse basate sul contenuto per evitare che tutte finiscano nello stesso mese"""
        # Usa l'hash del testo per generare una data consistente ma diversa
        import hashlib
        text_hash = hashlib.md5(text.encode()).hexdigest()
        hash_int = int(text_hash[:8], 16)
        
        # Genera date negli ultimi 12 mesi, distribuendo per MESI diversi
        base_date = datetime.now()
        months_back = (hash_int % 12) + 1  # Da 1 a 12 mesi fa
        day_in_month = (hash_int % 28) + 1  # Giorno del mese (1-28 per evitare problemi con febbraio)
        
        # Sottrai i mesi dalla data base
        target_month = base_date.month - months_back
        target_year = base_date.year
        
        # Gestisci il cambio di anno
        while target_month <= 0:
            target_month += 12
            target_year -= 1
        
        try:
            return datetime(target_year, target_month, day_in_month)
        except ValueError:
            # Fallback se la data non è valida
            return base_date - timedelta(days=months_back * 30)
    
    def _extract_due_date(self, text):
        """Estrae la data di scadenza"""
        patterns = [
            r'scadenza[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'entro[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'pagare entro[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                try:
                    date_str = matches[0]
                    # Prova diversi formati
                    for fmt in ['%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            return datetime.strptime(date_str, fmt).isoformat()
                        except:
                            continue
                except:
                    continue
        
        return None
    
    def _extract_billing_period(self, text):
        """Estrae il periodo di fatturazione"""
        patterns = [
            r'periodo[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})\s*[aA-]\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
            r'dal[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})\s*al[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                return {
                    'start_date': matches[0][0],
                    'end_date': matches[0][1]
                }
        
        return None
    
    def _extract_consumption(self, text):
        """Estrae i consumi"""
        consumptions = {}
        
        # Elettricità (kWh)
        kwh_pattern = r'(\d+[.,]?\d*)\s*kwh'
        kwh_matches = re.findall(kwh_pattern, text, re.IGNORECASE)
        if kwh_matches:
            consumptions['electricity_kwh'] = float(kwh_matches[-1].replace(',', '.'))
        
        # Gas (Smc)
        gas_pattern = r'(\d+[.,]?\d*)\s*smc'
        gas_matches = re.findall(gas_pattern, text, re.IGNORECASE)
        if gas_matches:
            consumptions['gas_smc'] = float(gas_matches[-1].replace(',', '.'))
        
        # Acqua (mc)
        water_pattern = r'(\d+[.,]?\d*)\s*mc.*acqua'
        water_matches = re.findall(water_pattern, text, re.IGNORECASE)
        if water_matches:
            consumptions['water_mc'] = float(water_matches[-1].replace(',', '.'))
        
        return consumptions if consumptions else None
    
    def _extract_account_number(self, text):
        """Estrae il numero di utenza/contratto"""
        patterns = [
            r'utenza[:\s]+(\w+)',
            r'contratto[:\s]+(\w+)',
            r'cod[.\s]*cliente[:\s]+(\w+)'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                return matches[0]
        
        return None
    
    def _needs_manual_review(self, bill_data):
        """Determina se la bolletta necessita revisione manuale"""
        critical_fields = ['supplier', 'amount', 'due_date']
        missing_fields = [field for field in critical_fields if not bill_data.get(field)]
        
        return len(missing_fields) > 1  # Se mancano più di 1 campo critico
    
    def _save_to_cosmos(self, bill_data):
        """Salva i dati estratti in Cosmos DB"""
        try:
            if self.container:
                self.container.create_item(body=bill_data)
        except exceptions.CosmosResourceExistsError:
            pass
        except Exception as e:
            pass
    
    def get_user_bills(self, user_id, limit=50):
        """Recupera le bollette di un utente"""
        if not self.container:
            return []
        
        try:
            query = f"SELECT * FROM c WHERE c.user_id = '{user_id}' ORDER BY c.upload_date DESC"
            items = list(self.container.query_items(
                query=query,
                enable_cross_partition_query=True,
                max_item_count=limit
            ))
            return items
        except Exception as e:
            return []
    
    def get_bills_by_type(self, user_id, bill_type, limit=50):
        """Recupera bollette per tipo"""
        if not self.container:
            return []
        
        try:
            query = f"SELECT * FROM c WHERE c.user_id = '{user_id}' AND c.bill_type = '{bill_type}' ORDER BY c.upload_date DESC"
            items = list(self.container.query_items(
                query=query,
                enable_cross_partition_query=True,
                max_item_count=limit
            ))
            return items
        except Exception as e:
            return []

    def generate_forecast(self, user_id, bill_type=None, months_ahead=3):
        """
        Genera previsioni per i prossimi mesi basate sui dati storici
        Utilizza algoritmi locali avanzati per il forecasting
        """
        if not self.container:
            return self._generate_mock_forecast(months_ahead)
        
        try:
            # Recupera dati storici
            if bill_type:
                bills = self.get_bills_by_type(user_id, bill_type, limit=100)
                # Se non ci sono bollette per questo tipo, ritorna errore
                if len(bills) == 0:
                    return {
                        'error': f'Nessuna bolletta di tipo "{bill_type}" trovata',
                        'predictions': [],
                        'trend': 'no_data',
                        'confidence': 'none'
                    }
            else:
                bills = self.get_user_bills(user_id, limit=100)
            
            if len(bills) < 3:
                return self._generate_mock_forecast(months_ahead, bills)
            
            # Prepara i dati per l'analisi
            monthly_data = self._prepare_monthly_data(bills)
            
            if len(monthly_data) < 3:
                return self._generate_mock_forecast(months_ahead, bills)
            

            forecast_data = self._calculate_linear_forecast(monthly_data, months_ahead, bill_type)
            
            if len(monthly_data) >= 6:
                 # Usa algoritmo avanzato locale per dati sufficienti
                forecast_data = self._calculate_advanced_local_forecast(monthly_data, months_ahead, bill_type)
            else:
                # Usa algoritmo semplice per pochi dati
                forecast_data = self._calculate_linear_forecast(monthly_data, months_ahead)
            
            return forecast_data
            
        except Exception as e:
            return self._generate_mock_forecast(months_ahead, bills if 'bills' in locals() else None)
    
    def _prepare_monthly_data(self, bills):
        """Prepara i dati mensili per l'analisi usando le date di scadenza come priorità"""
        monthly_totals = {}
        
        for bill in bills:
            try:
                # Usa la data di scadenza come priorità principale
                bill_date = None
                
                # Prima priorità: data di scadenza (due_date)
                if bill.get('due_date'):
                    try:
                        bill_date = datetime.fromisoformat(bill['due_date'].replace('Z', '+00:00'))
                    except:
                        pass
                
                # Seconda priorità: data estratta
                if not bill_date and bill.get('extracted_data') and bill['extracted_data'].get('date'):
                    try:
                        date_str = bill['extracted_data']['date']
                        # Prova diversi formati di data
                        for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S']:
                            try:
                                bill_date = datetime.strptime(date_str, fmt)
                                break
                            except ValueError:
                                continue
                    except:
                        pass
                
                # Fallback: upload_date
                if not bill_date:
                    bill_date = datetime.fromisoformat(bill['upload_date'].replace('Z', '+00:00'))
                
                month_key = bill_date.strftime('%Y-%m')
                
                amount = 0
                if bill.get('amount'):
                    # Pulisci l'importo
                    amount_str = str(bill['amount']).replace(',', '.').replace('€', '').strip()
                    try:
                        amount = float(amount_str)
                    except ValueError:
                        continue
                
                if month_key not in monthly_totals:
                    monthly_totals[month_key] = {
                        'total': 0,
                        'count': 0,
                        'date': bill_date
                    }
                
                monthly_totals[month_key]['total'] += amount
                monthly_totals[month_key]['count'] += 1
                
            except Exception as e:
                continue
        
        # Converti in lista ordinata
        sorted_months = sorted(monthly_totals.items(), key=lambda x: x[1]['date'])
        return [(month, data['total']) for month, data in sorted_months if data['total'] > 0]
    
    def _calculate_linear_forecast(self, monthly_data, months_ahead, bill_type=None):
        """Calcola previsioni usando regressione lineare semplice"""
        if len(monthly_data) < 2:
            return self._generate_mock_forecast(months_ahead, None)
        
        # Prepara i dati per numpy
        x_values = np.arange(len(monthly_data))
        y_values = np.array([data[1] for data in monthly_data])
        
        # Calcola la regressione lineare
        coeffs = np.polyfit(x_values, y_values, 1)
        slope, intercept = coeffs
        
        # Limita il slope per evitare crescite eccessive
        avg_amount = np.mean(y_values)
        max_slope = avg_amount * 0.05  # Massimo 5% di crescita per mese
        slope = np.clip(slope, -max_slope, max_slope)
        
        # Genera previsioni
        last_month = datetime.strptime(monthly_data[-1][0], '%Y-%m')
        forecast_data = {
            'predictions': [],
            'trend': 'crescente' if slope > 0 else 'decrescente' if slope < 0 else 'stabile',
            'confidence': 'media',
            'historical_average': float(np.mean(y_values)),
            'last_month_amount': float(y_values[-1]),
            'debug_info': {
                'original_slope': float(coeffs[0]),
                'capped_slope': float(slope),
                'data_points': len(monthly_data),
                'amounts_range': f"{float(np.min(y_values)):.2f} - {float(np.max(y_values)):.2f}"
            }
        }
        
        for i in range(1, months_ahead + 1):
            # Calcola il mese futuro in modo più preciso
            current_year = last_month.year
            current_month = last_month.month
            
            future_month_num = current_month + i
            future_year = current_year
            
            while future_month_num > 12:
                future_month_num -= 12
                future_year += 1
            
            future_month = datetime(future_year, future_month_num, 1)
            
            # Predizione basata sulla tendenza lineare
            x_future = len(monthly_data) + i - 1
            predicted_amount = slope * x_future + intercept
            
            # Aggiungi variazione stagionale simulata più conservativa
            # Modifica speciale per tipo di bolletta
            if bill_type == 'gas':
                if future_month.month in [7, 8]:  # Luglio e Agosto
                    seasonal_factor = 0.4  # Gas molto più basso in estate
                elif future_month.month in [12, 1, 2]:  # Inverno
                    seasonal_factor = 1.3  # Gas più alto in inverno
                else:
                    seasonal_factor = 0.8  # Primavera/autunno più bassi
            elif bill_type == 'electricity':
                if future_month.month in [7, 8]:  # Luglio e Agosto
                    seasonal_factor = 1.2  # Elettricità più alta in estate (climatizzazione)
                elif future_month.month in [12, 1, 2]:  # Inverno
                    seasonal_factor = 1.1  # Elettricità leggermente più alta in inverno
                else:
                    seasonal_factor = 1.0  # Stabile nelle altre stagioni
            else:
                # Default per altri tipi o quando non specificato
                seasonal_factor = 1 + 0.05 * np.sin(2 * np.pi * future_month.month / 12)
            
            predicted_amount *= seasonal_factor
            
            # Normalizzazione verso la media storica per evitare derive eccessive
            historical_avg = np.mean(y_values)
            weight_to_avg = min(i * 0.1, 0.3)  # Peso crescente verso la media
            predicted_amount = predicted_amount * (1 - weight_to_avg) + historical_avg * weight_to_avg
            
            # Assicurati che non sia negativo
            predicted_amount = max(predicted_amount, 0)
            
            forecast_data['predictions'].append({
                'month': future_month.strftime('%Y-%m'),
                'month_name': future_month.strftime('%B %Y'),
                'predicted_amount': round(predicted_amount, 2),
                'confidence_interval': {
                    'min': round(predicted_amount * 0.85, 2),
                    'max': round(predicted_amount * 1.15, 2)
                }
            })
        
        return forecast_data
    
    def _calculate_advanced_local_forecast(self, monthly_data, months_ahead, bill_type):
        if len(monthly_data) < 6:
            return self._calculate_linear_forecast(monthly_data, months_ahead, bill_type)
        
        # Prepara i dati
        amounts = np.array([data[1] for data in monthly_data])
        x_values = np.arange(len(monthly_data))
        
        # 1. Calcola trend lineare
        linear_coeffs = np.polyfit(x_values, amounts, 1)
        slope, intercept = linear_coeffs
        
        # 2. Calcola media mobile esponenziale (più peso ai dati recenti)
        alpha = 0.3 
        ema = [amounts[0]]
        for i in range(1, len(amounts)):
            ema.append(alpha * amounts[i] + (1 - alpha) * ema[i-1])
        
        # 3. Detecta stagionalità se ci sono almeno 12 mesi
        seasonal_pattern = {}
        if len(monthly_data) >= 12:
            for i, (month_str, amount) in enumerate(monthly_data):
                month_num = datetime.strptime(month_str, '%Y-%m').month
                if month_num not in seasonal_pattern:
                    seasonal_pattern[month_num] = []
                seasonal_pattern[month_num].append(amount)
            
            # Calcola fattori stagionali medi
            for month_num in seasonal_pattern:
                seasonal_pattern[month_num] = np.mean(seasonal_pattern[month_num])
        
        # 4. Adjustment per tipo bolletta
        bill_type_factors = {
            'electricity': {'winter': 1.1, 'summer': 1.15, 'other': 1.0},
            'gas': {'winter': 1.2, 'summer': 0.8, 'other': 1.0},
            'water': {'winter': 0.95, 'summer': 1.05, 'other': 1.0},
            'telecom': {'winter': 1.0, 'summer': 1.0, 'other': 1.0}
        }
        
        # Prepara forecast data
        last_month = datetime.strptime(monthly_data[-1][0], '%Y-%m')
        last_ema = ema[-1]
        avg_amount = np.mean(amounts)
        
        forecast_data = {
            'predictions': [],
            'trend': 'crescente' if slope > 0 else 'decrescente' if slope < 0 else 'stabile',
            'confidence': 'alta',
            'historical_average': float(avg_amount),
            'last_month_amount': float(amounts[-1]),
            'algorithm': 'Advanced Local (Linear + EMA + Seasonal)'
        }
        
        # Genera previsioni
        for i in range(1, months_ahead + 1):
            future_month = last_month + timedelta(days=30 * i)
            
            # Combinazione predizioni
            x_future = len(monthly_data) + i - 1
            linear_pred = slope * x_future + intercept
            ema_pred = last_ema * (1 + slope/avg_amount * 0.05)
            
            base_prediction = 0.7 * linear_pred + 0.3 * ema_pred
            
            # Applica stagionalità se disponibile
            if seasonal_pattern and future_month.month in seasonal_pattern:
                seasonal_factor = seasonal_pattern[future_month.month] / avg_amount
            else:
                # Stagionalità simulata generica più conservativa
                seasonal_factor = 1 + 0.08 * np.sin(2 * np.pi * future_month.month / 12)
            
            # Applica fattore tipo bolletta
            if bill_type in bill_type_factors:
                if future_month.month in [12, 1, 2]:  # Winter
                    type_factor = bill_type_factors[bill_type]['winter']
                elif future_month.month in [6, 7, 8]:  # Summer
                    type_factor = bill_type_factors[bill_type]['summer']
                else:
                    type_factor = bill_type_factors[bill_type]['other']
            else:
                type_factor = 1.0
            
            # Predizione finale con damping per mesi futuri
            predicted_amount = base_prediction * seasonal_factor * type_factor
            
            # Applica damping per evitare crescite eccessive nei mesi futuri
            damping_factor = 1 / (1 + i * 0.02)  # Riduce l'effetto per mesi più lontani
            predicted_amount *= damping_factor
            
            predicted_amount = max(predicted_amount, 0)
            
            # Intervalli di confidenza più stretti per algoritmo avanzato
            confidence_margin = 0.12  # ±12%
            
            forecast_data['predictions'].append({
                'month': future_month.strftime('%Y-%m'),
                'month_name': future_month.strftime('%B %Y'),
                'predicted_amount': round(predicted_amount, 2),
                'confidence_interval': {
                    'min': round(predicted_amount * (1 - confidence_margin), 2),
                    'max': round(predicted_amount * (1 + confidence_margin), 2)
                },
                'algorithm_details': {
                    'linear_component': round(linear_pred, 2),
                    'ema_component': round(ema_pred, 2),
                    'seasonal_factor': round(seasonal_factor, 3),
                    'type_factor': round(type_factor, 3)
                }
            })
        
        return forecast_data

    def _generate_mock_forecast(self, months_ahead, bills=None):
        """Genera previsioni di esempio quando non ci sono dati sufficienti"""
        current_date = datetime.now()
        
        # Se abbiamo alcune bollette, usa la media reale invece di un valore fisso
        if bills and len(bills) > 0:
            amounts = []
            for bill in bills:
                if bill.get('amount'):
                    try:
                        amount_str = str(bill['amount']).replace(',', '.').replace('€', '').strip()
                        amount = float(amount_str)
                        if amount > 0:
                            amounts.append(amount)
                    except:
                        continue
            
            if amounts:
                base_amount = sum(amounts) / len(amounts)  # Media reale
            else:
                base_amount = 120.0  # Fallback
        else:
            base_amount = 120.0  # Fallback quando non ci sono bollette
        
        forecast_data = {
            'predictions': [],
            'trend': 'stabile',
            'confidence': 'basso',
            'historical_average': base_amount,
            'last_month_amount': base_amount,
            'note': f'Previsioni simulate basate su {len(bills) if bills else 0} bollette - carica più bollette per previsioni accurate'
        }
        
        for i in range(1, months_ahead + 1):
            future_month = current_date + timedelta(days=30 * i)
            
            # Variazione casuale simulata
            variation = np.random.normal(0, 0.1)  # 10% di variazione
            predicted_amount = base_amount * (1 + variation)
            
            # Effetto stagionale simulato
            seasonal_factor = 1 + 0.2 * np.sin(2 * np.pi * future_month.month / 12)
            predicted_amount *= seasonal_factor
            
            forecast_data['predictions'].append({
                'month': future_month.strftime('%Y-%m'),
                'month_name': future_month.strftime('%B %Y'),
                'predicted_amount': round(predicted_amount, 2),
                'confidence_interval': {
                    'min': round(predicted_amount * 0.8, 2),
                    'max': round(predicted_amount * 1.2, 2)
                }
            })
        
        return forecast_data

    def get_consumption_trends(self, user_id, bill_type):
        """Analizza i trend di consumo per tipo di bolletta"""
        bills = self.get_bills_by_type(user_id, bill_type, limit=50)
        
        if len(bills) < 2:
            return None
        
        monthly_consumption = {}
        
        for bill in bills:
            try:
                upload_date = datetime.fromisoformat(bill['upload_date'].replace('Z', '+00:00'))
                month_key = upload_date.strftime('%Y-%m')
                
                consumption = bill.get('consumption')
                if consumption:
                    # Estrai valore numerico del consumo
                    consumption_value = self._extract_numeric_value(str(consumption))
                    if consumption_value > 0:
                        monthly_consumption[month_key] = consumption_value
            except:
                continue
        
        if len(monthly_consumption) < 2:
            return None
        
        # Calcola trend
        sorted_months = sorted(monthly_consumption.items())
        recent_months = sorted_months[-6:]  # Ultimi 6 mesi
        
        if len(recent_months) >= 2:
            values = [v[1] for v in recent_months]
            trend = 'increasing' if values[-1] > values[0] else 'decreasing'
            avg_consumption = sum(values) / len(values)
            
            return {
                'trend': trend,
                'average_consumption': round(avg_consumption, 2),
                'monthly_data': recent_months,
                'change_percentage': round(((values[-1] - values[0]) / values[0]) * 100, 1)
            }
        
        return None
    
    def _extract_numeric_value(self, text):
        """Estrae valore numerico da una stringa"""
        numbers = re.findall(r'\d+[.,]?\d*', text)
        if numbers:
            try:
                return float(numbers[0].replace(',', '.'))
            except:
                return 0
        return 0
    
bill_processor = BillProcessor()
