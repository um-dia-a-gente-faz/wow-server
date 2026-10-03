FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY transform.py item_icons.py app.py calibration.json ./

EXPOSE 9400
CMD ["python", "-u", "app.py"]
