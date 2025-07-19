# Usa Python 3.11 come base
FROM python:3.11-slim

# Imposta la directory di lavoro
WORKDIR /app

# Copia i requirements e installa le dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia tutti i file dell'applicazione
COPY . .

# Esponi la porta 8000
EXPOSE 8000

# Comando per avviare l'applicazione
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "600", "app:app"]
