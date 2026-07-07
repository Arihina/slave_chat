# Чат-агент 

По умолчанию сервис слушает `http://127.0.0.1:8002`

# Подготовка перед запуском

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

## Аутентификация

Сервис не управляет пользователями — это задача платформы (мастер-агент + Keycloak). Чат-агент получает UUID пользователя в заголовке `X-User-Id` и использует его как скоуп для своих данных. Заголовок обязателен **во всех** запросах:

```
X-User-Id: 11111111-1111-1111-1111-111111111111
```

JWT валидирует мастер-агент; сервис доверяет внутреннему трафику (должен быть закрыт снаружи в обход мастера). При переходе на валидацию JWT по JWKS Keycloak меняется только `user_id` в `deps.py`, эндпоинты не затрагиваются.

| Ситуация | Код |
|----------|-----|
| Заголовок `X-User-Id` отсутствует | `422` |
| `X-User-Id` не является валидным UUID | `400` |
| Обращение к чужому чату/сообщению | `404` |

Возврат `404` (а не `403`) для чужих объектов сознателен: сервис не подтверждает их существование.

Все идентификаторы (`session_id`, `message_id`) — UUID, генерируются на стороне сервиса.

## API

Все эндпоинты требуют заголовок `X-User-Id: <uuid>`. Списки и операции скоупятся по этому идентификатору; обращение к чужому ресурсу возвращает `404`.

### Сессии

#### `POST /sessions`
Создать новый чат (привязывается к `X-User-Id`).

Тело запроса (опционально):
```json
{ "title": "Название чата" }
```

Ответ `201 Created`:
```json
{
  "id": "b6b6f3a0-1c1a-4b0a-9c3a-2f1e8d7a6b5c",
  "title": "Название чата",
  "created_at": "2024-01-01T12:00:00+00:00",
  "updated_at": "2024-01-01T12:00:00+00:00"
}
```

---

#### `GET /sessions`
Список чатов текущего пользователя, отсортированных по `updated_at` (новые первые).

Ответ:
```json
[
  {
    "id": "b6b6f3a0-1c1a-4b0a-9c3a-2f1e8d7a6b5c",
    "title": "Тестовый чат",
    "created_at": "2024-01-01T12:00:00+00:00",
    "updated_at": "2024-01-01T12:05:00+00:00"
  }
]
```

---

#### `PATCH /sessions/{session_id}`
Переименовать чат.

Тело запроса:
```json
{ "title": "Новое название" }
```

Ответ: обновлённый объект сессии (см. формат выше).

---

#### `DELETE /sessions/{session_id}`
Удалить чат со всеми сообщениями и фидбэком (каскадно, по FK).

Ответ: `204 No Content`

---

### Сообщения

#### `GET /sessions/{session_id}/messages`
История сообщений чата.

Ответ:
```json
[
  {
    "id": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
    "role": "user",
    "content": "Привет!",
    "created_at": "2024-01-01T12:00:00+00:00"
  },
  {
    "id": "d2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f6a",
    "role": "assistant",
    "content": "Привет! Чем могу помочь?\n\nИсточники:\n- none",
    "created_at": "2024-01-01T12:00:05+00:00"
  }
]
```

---

#### `POST /sessions/{session_id}/chat`
Отправить сообщение. Ответ модели возвращается потоком (SSE).

Тело запроса:
```json
{ "message": "Привет!" }
```

Поток событий:
```
data: {"user_message_id": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"}
data: {"chunks": []}
data: {"token": "Привет"}
data: {"token": "! Чем"}
data: {"token": " могу помочь?"}
...
data: {"token": "\n\nИсточники:\n- none"}
data: {"message_id": "d2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f6a"}
data: [DONE]
```

Порядок событий:
- `user_message_id` — **первое** событие, id только что сохранённого сообщения пользователя
- `chunks` — всегда пустой список (у этого агента нет RAG; поле оставлено для единообразия формата с epoz)
- `token` — токены ответа LLM, включая служебный блок источников в конце (`- none`, т.к. источников нет)
- `message_id` — id сохранённого сообщения ассистента (`null`, если ответ модели оказался пустым)
- `error` — вместо `token`/`message_id`, если во время генерации произошла ошибка
- `[DONE]` — завершение потока

Если клиент обрывает соединение до завершения генерации, уже полученная часть ответа всё равно сохраняется в БД.

---

### Фидбэк

#### `POST /messages/{message_id}/feedback`
Сохранить произвольный JSON-фидбэк к сообщению ассистента. Формат `payload` не зафиксирован схемой — сервис хранит любой объект. Повторный вызов полностью перезаписывает `payload`.

Тело запроса (пример):
```json
{ "vote": 1, "comment": "Хороший ответ" }
```

Ответ:
```json
{
  "message_id": "d2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f6a",
  "payload": { "vote": 1, "comment": "Хороший ответ" }
}
```

---

#### `GET /messages/{message_id}/feedback`
Получить сохранённый фидбэк сообщения.

Ответ:
```json
{
  "message_id": "d2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f6a",
  "payload": { "vote": 1, "comment": "Хороший ответ" },
  "created_at": "2024-01-01T12:01:00+00:00",
  "updated_at": "2024-01-01T12:01:00+00:00"
}
```

`404`, если фидбэк ещё не оставлен.

---

#### `DELETE /messages/{message_id}/feedback`
Удалить фидбэк сообщения.

Ответ: `204 No Content` (`404`, если фидбэка не было).

---

### Служебное

#### `GET /healthz`
Проверка доступности сервиса и текущей модели.

Ответ:
```json
{ "status": "ok", "model": "qwen3.6:35b" }
```

---

## Примеры curl

Во всех запросах передаётся `X-User-Id`. Сервис слушает обычный HTTP (`-k` не нужен, в отличие от epoz).

```bash
U=11111111-1111-1111-1111-111111111111
BASE=http://localhost:8002

# Создать чат
curl -X POST $BASE/sessions \
  -H "X-User-Id: $U" \
  -H "Content-Type: application/json" \
  -d '{"title": "Тестовый чат"}'

# Список чатов
curl $BASE/sessions \
  -H "X-User-Id: $U"

# Отправить сообщение (SID — id сессии из ответа выше)
SID=b6b6f3a0-1c1a-4b0a-9c3a-2f1e8d7a6b5c
curl -X POST $BASE/sessions/$SID/chat \
  -H "X-User-Id: $U" \
  -H "Content-Type: application/json" \
  -d '{"message": "Привет!"}' \
  --no-buffer

# История сообщений
curl $BASE/sessions/$SID/messages \
  -H "X-User-Id: $U"

# Переименовать чат
curl -X PATCH $BASE/sessions/$SID \
  -H "X-User-Id: $U" \
  -H "Content-Type: application/json" \
  -d '{"title": "Новое название"}'

# Оставить фидбэк (MID — id сообщения ассистента из потока /chat)
MID=d2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f6a
curl -X POST $BASE/messages/$MID/feedback \
  -H "X-User-Id: $U" \
  -H "Content-Type: application/json" \
  -d '{"vote": 1, "comment": "Хороший ответ"}'

# Получить фидбэк
curl $BASE/messages/$MID/feedback \
  -H "X-User-Id: $U"

# Удалить фидбэк
curl -X DELETE $BASE/messages/$MID/feedback \
  -H "X-User-Id: $U"

# Удалить чат
curl -X DELETE $BASE/sessions/$SID \
  -H "X-User-Id: $U"

# Health-check
curl $BASE/healthz
```