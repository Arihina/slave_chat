# Чат-агент

Общий ассистент без RAG — обычный диалог поверх Ollama. По умолчанию сервис
слушает `http://127.0.0.1:8002`. API совместим с OpenAI Chat Completions
(`/v1/chat/completions`).

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
| Обращение к чужому completion'у, чату (`conversation`) или фидбэку | `404` |

Возврат `404` (а не `403`) для чужих объектов сознателен: сервис не подтверждает их существование.

## API — общая идея

API состоит из двух независимых частей:

- **`/v1/chat/completions`** — OpenAI-совместимый эндпоинт генерации. **Полностью stateless**: сервис не хранит и не переиспользует историю диалога — клиент присылает её целиком в `messages[]` при каждом запросе.
- **`/v1/platform/conversations`** — платформенное (не входящее в OpenAI-стандарт) расширение для UI: список чатов, история сообщений, переименование, удаление. Не участвует в генерации — это только группировка сообщений для отображения. Связь между двумя частями — необязательное поле `conversation_id` в теле запроса к `/v1/chat/completions`.

Сообщения персистятся в БД в любом случае (даже без `conversation_id`) — это нужно для фидбэка и повторного чтения (`GET /v1/chat/completions/{id}`), которые адресуются по `id` конкретного completion'а, а не по чату.

**У этого агента нет RAG** — источников/`sources`-ручки.

## База данных

PostgreSQL. Подключение настраивается через переменные окружения в `.env`.

Миграции управляются через Alembic. Применить:
```bash
alembic upgrade head
```

```
conversations                    платформенная сущность, только для UI/истории
├── id          UUID          PK, генерируется приложением (uuid4)
├── user_id     UUID          NOT NULL — владелец чата
├── title       TEXT          nullable (подставляется из первого вопроса, если не указан)
├── created_at  TIMESTAMPTZ
└── updated_at  TIMESTAMPTZ

messages
├── id                 UUID          PK, генерируется приложением (uuid4);
│                                    у ассистентских сообщений совпадает с
│                                    частью после "chatcmpl-" в id completion'а
├── user_id            UUID          NOT NULL — владелец записи
├── conversation_id    UUID          nullable, FK → conversations.id (ON DELETE CASCADE) —
│                                    НЕ участвует в сборке контекста для генерации,
│                                    только привязка к чату в UI-списке
├── role                TEXT          "user" | "assistant" | "system"
├── content             TEXT
├── model               VARCHAR(64)   значение "model" из запроса
├── prompt_tokens       INTEGER       nullable
├── completion_tokens   INTEGER       nullable
└── created_at          TIMESTAMPTZ

feedback
├── message_id  UUID    PK, FK → messages.id (ON DELETE CASCADE)
├── user_id     UUID    NOT NULL
├── payload     JSONB   произвольный объект, см. «Фидбэк» ниже
├── created_at  TIMESTAMPTZ
└── updated_at  TIMESTAMPTZ
```

Пользовательское сообщение сохраняется **до** начала генерации (не теряется,
даже если генерация оборвётся или упадёт с ошибкой) — ассистентское
сохраняется по завершении потока, либо частично при обрыве соединения
клиентом (`asyncio.CancelledError`), либо не сохраняется вовсе, если ответ
модели оказался пустым.

## Модель

Настраивается через `.env`:
```
OLLAMA_MODEL=qwen3.6:35b
```
Системный промпт (`system_prompt`, по умолчанию — универсальный ассистент
на русском) и лимит истории (`history_limit`, использовался только для
старой server-side сборки контекста и в stateless-режиме не участвует) —
настройки в `app/config.py`, не в `.env`.

## API

Все эндпоинты требуют заголовок `X-User-Id: <uuid>`. Ресурсы скоупятся по этому идентификатору; обращение к чужому ресурсу возвращает `404`.

### `POST /v1/chat/completions`

Генерация ответа. Клиент присылает **всю** историю диалога в `messages[]` — сервис её не хранит и не переиспользует между запросами. Сервис добавляет системный промпт (`settings.system_prompt`) первым сообщением перед отправкой в Ollama — это происходит на сервере и не настраивается через запрос.

Тело запроса:
```json
{
  "model": "chat",
  "messages": [
    {"role": "user", "content": "привет"},
    {"role": "assistant", "content": "привет! чем помочь?"},
    {"role": "user", "content": "расскажи анекдот"}
  ],
  "stream": true,
  "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```
| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `model` | string | нет (по умолчанию `"chat"`) | не влияет на поведение, только эхом в ответе |
| `messages` | array | да | последнее сообщение — `role: "user"`, это и есть текущий вопрос |
| `stream` | bool | нет (по умолчанию `false`) | стримить ответ через SSE |
| `conversation_id` | UUID string | нет | платформенное расширение — привязать сообщение к чату из `/v1/platform/conversations`. Чужой/несуществующий `conversation_id` → `404` |

**Нестрим-ответ** (`chat.completion`):
```json
{
  "id": "chatcmpl-1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "object": "chat.completion",
  "created": 1735900000,
  "model": "chat",
  "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Заходит как-то...\n\nИсточники:\n- none"},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 128, "completion_tokens": 42, "total_tokens": 170}
}
```

**Стрим-ответ** (`stream: true`) — SSE, `chat.completion.chunk`, один и тот же `id` во всех чанках:
```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1735900000,"model":"chat","conversation_id":"3fa85f64-...","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1735900000,"model":"chat","conversation_id":"3fa85f64-...","choices":[{"index":0,"delta":{"content":"Заходит"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1735900000,"model":"chat","conversation_id":"3fa85f64-...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```
`conversation_id` в ответе — `null`, если не был передан в запросе. `id` (`chatcmpl-<uuid>`) — ключ для фидбэка и повторного чтения ниже. Если модель вернула пустой ответ — сообщение ассистента не сохраняется, но клиент всё равно получает валидный `id` в ответе (просто по нему ничего не найдётся при последующих обращениях).

Если клиент обрывает соединение до завершения генерации, уже полученная часть ответа всё равно сохраняется в БД (без блока «Источники»).

---

### `GET /v1/chat/completions/{completion_id}`

Получить ранее сгенерированный ответ повторно — по `id`, который пришёл в `POST`-ответе (`chatcmpl-<uuid>` целиком или голый UUID). Ответ — тот же `chat.completion`-объект, что и у `POST` в нестрим-режиме, восстановленный из БД (включая `usage`).

`404`, если `id` не найден, принадлежит не вам, либо указывает на сообщение с `role: "user"` (в норме такой `id` клиенту никогда не отдаётся).

---

### `POST/GET/DELETE /v1/chat/completions/{completion_id}/feedback`

Произвольный JSON-фидбэк к сообщению ассистента. Формат `payload` не зафиксирован схемой — сервис хранит любой объект. **Повторный `POST` мёржит** новый `payload` с уже сохранённым на уровне JSONB-ключей (`payload || new_payload` — новые ключи перекрывают старые с тем же именем, остальные ключи старого payload сохраняются), а не перезаписывает целиком.

Тело `POST`-запроса (пример):
```json
{ "vote": 1, "comment": "Хороший ответ" }
```

Ответ (`POST`):
```json
{
  "message_id": "1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "payload": { "vote": 1, "comment": "Хороший ответ" }
}
```

Ответ (`GET`):
```json
{
  "message_id": "1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "payload": { "vote": 1, "comment": "Хороший ответ" },
  "created_at": "2026-08-03T12:01:00",
  "updated_at": "2026-08-03T12:01:00"
}
```
`404`, если фидбэк ещё не оставлен. `DELETE` удаляет запись целиком, `204` (`404`, если фидбэка не было).

---

### Чаты — `/v1/platform/conversations`

#### `POST /v1/platform/conversations`
Создать новый чат. Тело (опционально): `{ "title": "Название чата" }`.

Ответ `201`:
```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "title": "Название чата",
  "created_at": "2026-08-03T12:00:00",
  "updated_at": "2026-08-03T12:00:00"
}
```

#### `GET /v1/platform/conversations`
Список чатов текущего пользователя, отсортированных по `updated_at` (новые первые). Формат элемента — как у `POST`.

#### `GET /v1/platform/conversations/{id}/messages`
История сообщений чата вместе с фидбэком — для восстановления `messages[]` на фронте при открытии чата.
```json
[
  {
    "id": "9c858901-8a57-4791-81fe-4c455b099bc9",
    "role": "user",
    "content": "привет",
    "created_at": "2026-08-03T12:00:00",
    "feedback": null
  },
  {
    "id": "1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
    "role": "assistant",
    "content": "привет! чем помочь?\n\nИсточники:\n- none",
    "created_at": "2026-08-03T12:00:05",
    "feedback": {"payload": {"vote": 1, "comment": "Хороший ответ"}}
  }
]
```
`id` ассистентского сообщения — тот же UUID, что использовать для `/v1/chat/completions/{id}/feedback` и повторного чтения.

#### `PATCH /v1/platform/conversations/{id}`
Переименовать чат. Тело: `{ "title": "Новое название" }`.

#### `DELETE /v1/platform/conversations/{id}`
Удалить чат со всеми сообщениями и их фидбэком (каскадно). Ответ: `204 No Content`.

---

### Служебное

#### `GET /healthz`
Проверка доступности сервиса и текущей модели. Не часть чат-контракта, без `X-User-Id`.
```json
{ "status": "ok", "model": "qwen3.6:35b" }
```

## Ошибки

Единый формат вместо FastAPI-дефолта `{"detail": ...}`:
```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": null}}
```
`type` — грубая классификация по HTTP-статусу: `400/413/415/422` → `invalid_request_error`, `401` → `authentication_error`, `404` → `not_found_error`, остальное → `server_error`. Ошибки соединения с Ollama во время генерации — до первого чанка `502`, в середине уже начатого стрима — `data: {"error": {...}}` перед завершением потока.

## Примеры curl

```bash
U=11111111-1111-1111-1111-111111111111
BASE=http://localhost:8002

# --- генерация, без привязки к чату ---
curl -X POST $BASE/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "chat", "messages": [{"role": "user", "content": "привет"}]}'

# то же самое, стримом
curl -N -X POST $BASE/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "chat", "stream": true, "messages": [{"role": "user", "content": "расскажи анекдот"}]}'

ID=chatcmpl-1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2

# получить ответ повторно по id
curl $BASE/v1/chat/completions/$ID -H "X-User-Id: $U"

# --- фидбэк ---
curl -X POST $BASE/v1/chat/completions/$ID/feedback \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"vote": 1, "comment": "Хороший ответ"}'
curl $BASE/v1/chat/completions/$ID/feedback -H "X-User-Id: $U"
curl -X DELETE $BASE/v1/chat/completions/$ID/feedback -H "X-User-Id: $U"

# --- чаты (платформенный CRUD) ---
curl -X POST $BASE/v1/platform/conversations \
  -H "X-User-Id: $U" -H "Content-Type: application/json" -d '{"title": "Тестовый чат"}'

CID=3fa85f64-5717-4562-b3fc-2c963f66afa6

# сообщение внутри чата — conversation_id привязывает запись к нему
curl -X POST $BASE/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d "{\"model\": \"chat\", \"conversation_id\": \"$CID\", \"messages\": [{\"role\": \"user\", \"content\": \"привет\"}]}"

curl $BASE/v1/platform/conversations -H "X-User-Id: $U"
curl $BASE/v1/platform/conversations/$CID/messages -H "X-User-Id: $U"

curl -X PATCH $BASE/v1/platform/conversations/$CID \
  -H "X-User-Id: $U" -H "Content-Type: application/json" -d '{"title": "Новое название"}'
curl -X DELETE $BASE/v1/platform/conversations/$CID -H "X-User-Id: $U"

# --- служебное ---
curl $BASE/healthz
```