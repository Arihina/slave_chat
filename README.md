## База данных
```
docker compose up -d
```

## Зависимости
```
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt
```

Настройка `.env`
```
DB_HOST=localhost
DB_PORT=5434
DB_USER=postgres
DB_PASSWORD=postgres
DB_NAME=chat_db
OLLAMA_MODEL=qwen3.6:35b
```

## Миграции
```
alembic upgrade head
```

## Запуск
```
python3 main.py
```
