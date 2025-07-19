from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session
import os
import time
import io
import hashlib
from datetime import datetime
from werkzeug.utils import secure_filename
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient
from dotenv import load_dotenv
import msal
from flask_session import Session
from functools import wraps
from bill_processor import bill_processor

# Carica le variabili d'ambiente
load_dotenv()

# Check if we're in testing mode
TESTING = os.getenv('TESTING', 'False').lower() == 'true'

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'development-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Configurazione Flask Session per Azure Web App
# In Azure Web App, il filesystem potrebbe essere read-only, quindi usiamo sessioni in memoria
if os.getenv('WEBSITE_SITE_NAME'):  # Rileva se siamo su Azure Web App
    app.config['SESSION_TYPE'] = 'null'  # Usa le sessioni Flask standard
else:
    app.config['SESSION_TYPE'] = 'filesystem'  # Per sviluppo locale
    
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'pdf_app:'
Session(app)

# Configurazione Azure AD B2C
AZURE_B2C_TENANT_NAME = os.getenv('AZURE_B2C_TENANT_NAME')
AZURE_B2C_CLIENT_ID = os.getenv('AZURE_B2C_CLIENT_ID')
AZURE_B2C_CLIENT_SECRET = os.getenv('AZURE_B2C_CLIENT_SECRET')
AZURE_B2C_TENANT_ID = os.getenv('AZURE_B2C_TENANT_ID')
AZURE_B2C_POLICY_NAME = os.getenv('AZURE_B2C_POLICY_NAME')
AZURE_B2C_AUTHORITY = os.getenv('AZURE_B2C_AUTHORITY')
AZURE_B2C_REDIRECT_URI = os.getenv('AZURE_B2C_REDIRECT_URI')

# Scope per Azure AD B2C - usa scopes minimi per evitare problemi
AZURE_B2C_SCOPES = []  # Azure AD B2C può funzionare senza scopes espliciti

# Configurazione Azure Storage (OBBLIGATORIA per produzione)
AZURE_STORAGE_CONNECTION_STRING = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
AZURE_CONTAINER_NAME = os.getenv('AZURE_BLOB_CONTAINER_NAME')

# Verifica configurazioni Azure AD B2C
azure_b2c_configured = bool(AZURE_B2C_CLIENT_ID and AZURE_B2C_CLIENT_SECRET and AZURE_B2C_TENANT_ID and AZURE_B2C_AUTHORITY)
if not azure_b2c_configured:
    print("ATTENZIONE: Azure AD B2C non configurato!")
    print("L'app funzionerà senza autenticazione (modalità sviluppo)")
    print("Per la produzione, configura Azure AD B2C per permettere registrazione/login degli utenti")

# Verifica e inizializza Azure Storage
if TESTING:
    # Durante i test, non inizializziamo Azure
    blob_service_client = None
    container_client = None
elif not AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_CONNECTION_STRING == 'DefaultEndpointsProtocol=https;AccountName=your_storage_account_name;AccountKey=your_storage_account_key;EndpointSuffix=core.windows.net':
    print("ATTENZIONE: Configurazioni Azure mancanti!")
    print("Modifica il file .env con le tue credenziali Azure reali")
    print("L'app funzionerà solo quando Azure sarà configurato correttamente")
    blob_service_client = None
    container_client = None
else:
    try:
        # Inizializza Azure Blob Service Client
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
        # Crea il container se non esiste
        try:
            container_client.create_container()
            print(f"Container '{AZURE_CONTAINER_NAME}' creato con successo.")
        except Exception as e:
            if "ContainerAlreadyExists" in str(e):
                print(f"Container '{AZURE_CONTAINER_NAME}' già esistente.")
            else:
                print(f"Errore nella creazione del container: {e}")
                blob_service_client = None
                container_client = None
              
        if container_client:
            print(f"Applicazione configurata con Azure Blob Storage - Container: {AZURE_CONTAINER_NAME}")        
    except Exception as e:
        print(f"Errore nella connessione ad Azure: {e}")
        print("Verifica le credenziali Azure nel file .env")
        blob_service_client = None
        container_client = None


# Funzioni di autenticazione
def get_msal_app():
    """Crea un'istanza MSAL per Azure AD B2C"""
    if not azure_b2c_configured:
        return None
    return msal.ConfidentialClientApplication(
        AZURE_B2C_CLIENT_ID,
        authority=AZURE_B2C_AUTHORITY,
        client_credential=AZURE_B2C_CLIENT_SECRET,
    )

def get_user_folder(user_id):
    """Genera un nome cartella sicuro per l'utente"""
    if not user_id:
        return "anonymous"
    # Crea un hash dell'user ID per privacy e sicurezza
    return f"user_{hashlib.md5(user_id.encode()).hexdigest()[:12]}"

def login_required(f):
    """Decorator per richiedere autenticazione"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not azure_b2c_configured:
            # Modalità sviluppo senza autenticazione
            return f(*args, **kwargs)
        
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """Ottiene l'utente corrente dalla sessione"""
    if not azure_b2c_configured:
        # Modalità sviluppo - utente fittizio
        return {
            'id': 'dev-user',
            'name': 'Development User',
            'email': 'dev@localhost',
            'folder': 'dev-user'
        }
    
    if 'user' in session:
        user_data = session['user']
        
        # Azure AD B2C restituisce i dati in modo leggermente diverso
        user_id = user_data.get('oid', user_data.get('sub', 'unknown'))
        
        # Gestisci il nome utente
        user_name = user_data.get('name', 'Utente')
        if user_name == 'unknown':
            # Se il nome è 'unknown', prova a estrarlo dall'email
            emails = user_data.get('emails', [])
            if emails and len(emails) > 0:
                email = emails[0]
                user_name = email.split('@')[0].capitalize()
            else:
                user_name = 'Nuovo Utente'
        
        # Gestisci l'email
        emails = user_data.get('emails', [])
        user_email = emails[0] if emails and len(emails) > 0 else 'no-email@example.com'
        
        return {
            'id': user_id,
            'name': user_name,
            'email': user_email,
            'folder': get_user_folder(user_id)
        }
    return None

ALLOWED_EXTENSIONS = {'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def upload_file_to_azure(file, filename, user_folder=None):
    """Carica un file su Azure Blob Storage nella cartella dell'utente"""
    if not container_client:
        return {'success': False, 'error': 'Azure Blob Storage non configurato'}
    
    try:
        # Crea il nome del blob con la cartella dell'utente
        if user_folder:
            blob_name = f"{user_folder}/{filename}"
        else:
            blob_name = filename
            
        blob_client = container_client.get_blob_client(blob_name)
        file.seek(0)  # Reset del puntatore del file
        blob_client.upload_blob(file.read(), overwrite=True)
        
        # Ottieni le proprietà del blob
        blob_properties = blob_client.get_blob_properties()
        return {
            'success': True,
            'size': blob_properties.size,
            'last_modified': blob_properties.last_modified,
            'blob_name': blob_name
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def download_file_from_azure(filename, user_folder=None):
    """Scarica un file da Azure Blob Storage dalla cartella dell'utente"""
    if not container_client:
        return None
        
    try:
        # Crea il nome del blob con la cartella dell'utente
        if user_folder:
            blob_name = f"{user_folder}/{filename}"
        else:
            blob_name = filename
            
        blob_client = container_client.get_blob_client(blob_name)
        blob_data = blob_client.download_blob()
        
        # Crea un oggetto BytesIO per il download
        file_stream = io.BytesIO()
        blob_data.readinto(file_stream)
        file_stream.seek(0)
        
        return file_stream
    except Exception as e:
        return None

def delete_file_from_azure(filename, user_folder=None):
    """Elimina un file da Azure Blob Storage dalla cartella dell'utente"""
    if not container_client:
        return False
        
    try:
        # Crea il nome del blob con la cartella dell'utente
        if user_folder:
            blob_name = f"{user_folder}/{filename}"
        else:
            blob_name = filename
            
        blob_client = container_client.get_blob_client(blob_name)
        blob_client.delete_blob()
        return True
    except Exception as e:
        return False

def get_file_info(filename, user_folder=None):
    """Ottiene informazioni su un file caricato da Azure"""
    if not container_client:
        return None
        
    try:
        # Crea il nome del blob con la cartella dell'utente
        if user_folder:
            blob_name = f"{user_folder}/{filename}"
        else:
            blob_name = filename
            
        blob_client = container_client.get_blob_client(blob_name)
        blob_properties = blob_client.get_blob_properties()
        
        return {
            'name': filename,  # Nome originale senza il percorso
            'blob_name': blob_name,  # Nome completo del blob
            'size': blob_properties.size,
            'size_mb': round(blob_properties.size / (1024 * 1024), 2),
            'upload_time': blob_properties.last_modified.replace(tzinfo=None),
            'formatted_time': blob_properties.last_modified.strftime('%d/%m/%Y %H:%M:%S'),
            'location': 'Azure Blob Storage'
        }
    except Exception as e:
        return None

def get_uploaded_files(user_folder=None):
    """Ottiene la lista di tutti i file PDF caricati dall'utente da Azure"""
    files = []
    
    if not container_client:
        return files
    
    try:
        blob_list = container_client.list_blobs()
        for blob in blob_list:
            # Se è specificata una cartella utente, filtra solo i blob di quella cartella
            if user_folder:
                if not blob.name.startswith(f"{user_folder}/"):
                    continue
                # Estrai il nome del file dalla path completa
                filename = blob.name[len(f"{user_folder}/"):]
            else:
                filename = blob.name
                
            if filename.lower().endswith('.pdf'):
                file_info = get_file_info(filename, user_folder)
                if file_info:
                    files.append(file_info)
    except Exception as e:
        pass
    
    return sorted(files, key=lambda x: x['upload_time'], reverse=True)

# Route di autenticazione
@app.route("/login")
def login():
    """Inizia il processo di login/registrazione con Azure AD B2C"""
    if not azure_b2c_configured:
        flash("Azure AD B2C non è configurato. Accesso in modalità sviluppo.", "warning")
        return redirect(url_for('home'))
    
    msal_app = get_msal_app()
    if not msal_app:
        flash("Errore nella configurazione dell'autenticazione", "error")
        return redirect(url_for('home'))
    
    # Costruisci l'URL di autorizzazione per Azure AD B2C
    auth_url = msal_app.get_authorization_request_url(
        AZURE_B2C_SCOPES,
        redirect_uri=AZURE_B2C_REDIRECT_URI
    )
    
    return redirect(auth_url)

@app.route("/auth/callback")
def auth_callback():
    """Gestisce il callback dopo il login/registrazione Azure AD B2C"""
    if not azure_b2c_configured:
        return redirect(url_for('home'))
    
    msal_app = get_msal_app()
    if not msal_app:
        flash("Errore nella configurazione dell'autenticazione", "error")
        return redirect(url_for('home'))
    
    # Ottieni il token dal codice di autorizzazione
    if request.args.get('code'):
        try:
            result = msal_app.acquire_token_by_authorization_code(
                request.args['code'],
                scopes=AZURE_B2C_SCOPES,
                redirect_uri=AZURE_B2C_REDIRECT_URI
            )
            
            # Per Azure AD B2C, controlla se ci sono id_token_claims (invece di access_token)
            if "id_token_claims" in result and result["id_token_claims"]:
                # Salva le informazioni utente nella sessione
                session['user'] = result.get("id_token_claims")
                user = get_current_user()
                
                # Controlla se è un nuovo utente basandoci sui claims
                user_claims = result.get("id_token_claims", {})
                if user_claims.get('newUser') or user_claims.get('name') == 'unknown':
                    flash(f"Benvenuto nella tua nuova area, {user['name']}! 🎉", "success")
                else:
                    flash(f"Bentornato, {user['name']}! 👋", "success")
                    
                return redirect(url_for('dashboard'))
            else:
                # Debug: mostra cosa è stato restituito
                if result.get("error"):
                    flash(f"Errore B2C: {result.get('error_description', result.get('error'))}", "error")
                else:
                    flash("Autenticazione riuscita ma nessun token utente trovato", "warning")
                
        except Exception as e:
            flash(f"Errore durante l'autenticazione / exeption: {str(e)}", "error")
    
    return redirect(url_for('home'))

@app.route("/logout")
def logout():
    """Effettua il logout"""
    user = get_current_user()
    if user:
        flash(f"Arrivederci, {user['name']}!", "info")
    
    session.clear()
    
    if azure_b2c_configured:
        # Redirect al logout di Azure AD B2C
        logout_url = f"{AZURE_B2C_AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={url_for('home', _external=True)}"
        return redirect(logout_url)
    
    return redirect(url_for('home'))

@app.route("/")
def home():
    user = get_current_user()
    return render_template("home.html", user=user, azure_b2c_configured=azure_b2c_configured)


@app.route("/upload")
@login_required
def upload_page():
    user = get_current_user()
    return render_template("upload.html", user=user)


@app.route("/upload", methods=['POST'])
@login_required
def upload_file():
    try:
        if not container_client:
            return jsonify({'success': False, 'error': 'Azure Blob Storage non configurato. Contatta l\'amministratore.'})
            
        user = get_current_user()
        if not user:
            return jsonify({'success': False, 'error': 'Utente non autenticato'})
            
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Nessun file selezionato'})
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Nessun file selezionato'})
        
        if file and allowed_file(file.filename):
            # Crea un nome file sicuro con timestamp
            filename = secure_filename(file.filename)
            timestamp = str(int(time.time()))
            name, ext = os.path.splitext(filename)
            filename = f"{name}_{timestamp}{ext}"
            
            # Upload del file su Azure nella cartella dell'utente
            upload_result = upload_file_to_azure(file, filename, user['folder'])
            
            if upload_result['success']:
                # Processa la bolletta per estrarre i dati
                try:
                    file.seek(0)  # Reset del puntatore del file
                    bill_data = bill_processor.extract_bill_data(
                        file, 
                        filename, 
                        user['id']
                    )
                    
                    message = f'File "{filename}" caricato con successo!'
                    if bill_data.get('manual_review_needed'):
                        message += ' ⚠️ Revisione manuale richiesta per alcuni dati.'
                    else:
                        bill_type = bill_data.get('bill_type', 'unknown')
                        supplier = bill_data.get('supplier', 'N/D')
                        amount = bill_data.get('amount')
                        message += f' 📄 Tipo: {bill_type.title()}, Fornitore: {supplier}'
                        if amount:
                            message += f', Importo: €{amount:.2f}'
                    
                    return jsonify({
                        'success': True, 
                        'message': message,
                        'filename': filename,
                        'storage': 'azure',
                        'bill_data': {
                            'type': bill_data.get('bill_type'),
                            'supplier': bill_data.get('supplier'),
                            'amount': bill_data.get('amount'),
                            'due_date': bill_data.get('due_date'),
                            'needs_review': bill_data.get('manual_review_needed', False)
                        }
                    })
                except Exception as e:
                    return jsonify({
                        'success': True, 
                        'message': f'File "{filename}" caricato, ma errore nell\'estrazione dati: {str(e)}',
                        'filename': filename,
                        'storage': 'azure'
                    })
            else:
                return jsonify({'success': False, 'error': f'Errore durante l\'upload: {upload_result["error"]}'})
        else:
            return jsonify({'success': False, 'error': 'Solo file PDF sono ammessi'})
            
    except Exception as e:
        return jsonify({'success': False, 'error': f'Errore durante l\'upload: {str(e)}'})


@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    files = get_uploaded_files(user['folder'])
    total_files = len(files)
    total_size = sum(file['size'] for file in files)
    total_size_mb = round(total_size / (1024 * 1024), 2)
    
    storage_info = {
        'mode': 'azure',
        'container': AZURE_CONTAINER_NAME,
        'connected': container_client is not None,
        'user_folder': user['folder']
    }
    
    return render_template("dashboard.html", 
                         files=files, 
                         total_files=total_files,
                         total_size_mb=total_size_mb,
                         storage_info=storage_info,
                         user=user)


@app.route("/download/<filename>")
@login_required
def download_file(filename):
    try:
        if not container_client:
            flash('Azure Blob Storage non configurato', 'danger')
            return redirect(url_for('dashboard'))
        
        user = get_current_user()
        file_stream = download_file_from_azure(filename, user['folder'])
        
        if file_stream:
            return send_file(
                file_stream,
                as_attachment=True,
                download_name=filename,
                mimetype='application/pdf'
            )
        else:
            flash(f'File "{filename}" non trovato nella tua area', 'danger')
            return redirect(url_for('dashboard'))
            
    except Exception as e:
        flash(f'Errore durante il download: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))


@app.route("/delete/<filename>", methods=['POST'])
@login_required
def delete_file(filename):
    try:
        if not container_client:
            return jsonify({'success': False, 'error': 'Azure Blob Storage non configurato'})
        
        user = get_current_user()
        if delete_file_from_azure(filename, user['folder']):
            return jsonify({'success': True, 'message': f'File "{filename}" eliminato con successo dalla tua area!'})
        else:
            return jsonify({'success': False, 'error': 'File non trovato nella tua area o errore nell\'eliminazione'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Errore durante l\'eliminazione: {str(e)}'})


@app.route("/bills")
@login_required
def bills_management():
    """Dashboard per la gestione delle bollette"""
    user = get_current_user()
    
    # Recupera le bollette dell'utente
    bills = bill_processor.get_user_bills(user['id'])
    
    # Statistiche
    total_bills = len(bills)
    bills_needing_review = len([b for b in bills if b.get('manual_review_needed', False)])
    
    # Raggruppa per tipo
    bills_by_type = {}
    total_amounts_by_type = {}
    
    for bill in bills:
        bill_type = bill.get('bill_type', 'unknown')
        if bill_type not in bills_by_type:
            bills_by_type[bill_type] = []
            total_amounts_by_type[bill_type] = 0
        
        bills_by_type[bill_type].append(bill)
        
        if bill.get('amount'):
            total_amounts_by_type[bill_type] += bill['amount']
    
    stats = {
        'total_bills': total_bills,
        'bills_needing_review': bills_needing_review,
        'bills_by_type': bills_by_type,
        'total_amounts_by_type': total_amounts_by_type
    }
    
    return render_template("bills_management.html", 
                         bills=bills, 
                         stats=stats,
                         user=user)

@app.route("/bills/api/list")
@login_required
def api_bills_list():
    """API per ottenere la lista delle bollette"""
    user = get_current_user()
    bill_type = request.args.get('type', 'all')
    
    if bill_type == 'all':
        bills = bill_processor.get_user_bills(user['id'])
    else:
        bills = bill_processor.get_bills_by_type(user['id'], bill_type)
    
    return jsonify({'bills': bills})

@app.route("/bills/api/stats")
@login_required
def api_bills_stats():
    """API per statistiche delle bollette"""
    user = get_current_user()
    bills = bill_processor.get_user_bills(user['id'])
    
    # Calcola statistiche per grafici usando le date di scadenza
    monthly_stats = {}
    type_stats = {}
    
    for index, bill in enumerate(bills):
        # Usa la data di scadenza come priorità principale per i grafici
        bill_date = None
        
        # Prima priorità: data di scadenza (due_date)
        if bill.get('due_date'):
            try:
                # due_date è già in formato ISO dalla estrazione
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
        if not bill_date and bill.get('upload_date'):
            try:
                upload_date_str = bill['upload_date']
                bill_date = datetime.fromisoformat(upload_date_str.replace('Z', '+00:00'))
            except Exception as e:
                continue
        
        if bill_date:
            month_key = bill_date.strftime('%Y-%m')
            
            if month_key not in monthly_stats:
                monthly_stats[month_key] = {'count': 0, 'total_amount': 0}
            
            monthly_stats[month_key]['count'] += 1
            if bill.get('amount'):
                try:
                    amount = float(str(bill['amount']).replace(',', '.').replace('€', '').strip())
                    monthly_stats[month_key]['total_amount'] += amount
                except:
                    pass
        
        # Stats per tipo
        bill_type = bill.get('bill_type', 'unknown')
        if bill_type not in type_stats:
            type_stats[bill_type] = {'count': 0, 'total_amount': 0}
        
        type_stats[bill_type]['count'] += 1
        if bill.get('amount'):
            try:
                amount = float(str(bill['amount']).replace(',', '.').replace('€', '').strip())
                type_stats[bill_type]['total_amount'] += amount
            except:
                pass
    
    return jsonify({
        'monthly_stats': monthly_stats,
        'type_stats': type_stats
    })

@app.route("/bills/api/forecast")
@login_required
def api_forecast():
    """API per previsioni delle bollette"""
    user = get_current_user()
    bill_type = request.args.get('type')  # Opzionale: filtra per tipo
    months_ahead = int(request.args.get('months', 3))  # Default 3 mesi
    
    try:
        forecast_data = bill_processor.generate_forecast(user['id'], bill_type, months_ahead)
        
        # Se c'è un errore (es. nessuna bolletta per quel tipo), ritorna l'errore
        if forecast_data.get('error'):
            return jsonify({
                'error': forecast_data['error'],
                'predictions': [],
                'message': f'Nessun dato disponibile per "{bill_type}"' if bill_type else 'Nessun dato disponibile'
            }), 400
        
        return jsonify(forecast_data)
        
    except Exception as e:
        return jsonify({
            'error': 'Errore nel calcolo delle previsioni',
            'predictions': [],
            'message': str(e)
        }), 500

@app.route("/bills/api/trends")
@login_required
def api_consumption_trends():
    """API per trend di consumo"""
    user = get_current_user()
    bill_type = request.args.get('type', 'electricity')  # Default electricity
    
    trends = bill_processor.get_consumption_trends(user['id'], bill_type)
    
    if trends:
        return jsonify(trends)
    else:
        return jsonify({
            'error': 'Dati insufficienti per calcolare i trend',
            'message': 'Carica più bollette per visualizzare i trend di consumo'
        })


if __name__ == "__main__":
    # Per Azure Web App, usa le variabili d'ambiente per porta e host
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

