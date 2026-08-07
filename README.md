# Чат-агент

Общий ассистент без RAG — обычный диалог поверх Ollama. По умолчанию сервис
слушает `http://127.0.0.1:8002`. Отдаёт генерацию в **двух** формах OpenAI
API одновременно — Chat Completions и Responses — реализован по тому же
контракту, что и epoz, см. `epoz/README.md` для сравнения.

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
| Обращение к чужому completion'у/response'у, чату (`conversation`) или фидбэку | `404` |

Возврат `404` (а не `403`) для чужих объектов сознателен: сервис не подтверждает их существование.

## API — общая идея

Генерация доступна в двух формах одновременно, плюс платформенное расширение для UI:

- **`/v1/chat/completions`** — форма Chat Completions. **Полностью stateless**: клиент присылает всю историю в `messages[]` при каждом запросе. Путь для быстрой интеграции сторонних клиентов/вендоров и для отладки.
- **`/v1/responses`** — форма Responses API. Тоже работает stateless (весь `input` от клиента) — но при переданном `conversation_id` **сервис сам собирает историю из БД** (последние `HISTORY_LIMIT` сообщений чата), клиенту нужно прислать только новый ход. Это путь, которым пользуется собственный фронт платформы. Подробности — в разделе `POST /v1/responses` ниже.
- **`/v1/platform/conversations`** — платформенное (не входящее ни в одну спеку OpenAI) расширение для UI: список чатов, история сообщений, переименование, удаление. Обе формы генерации читают/пишут в одни и те же `conversations`/`messages`.

Фидбэк (`/v1/chat/completions/{id}/feedback`) — **общий для обеих форм**, не дублируется под `/v1/responses/...`: сообщение, сгенерированное через `/v1/responses`, получает `id` вида `resp_<uuid>`, но фидбэк к нему адресуется по тому же общему пути, что и `chatcmpl-`-сообщения — сервис принимает оба префикса одинаково.

Сообщения персистятся в БД в любом случае (даже без `conversation_id`, в любой из форм) — это нужно для фидбэка и повторного чтения, которые адресуются по `id` конкретного сообщения, а не по чату.

**У этого агента нет RAG** — источников/`sources`-ручки, в отличие от epoz, здесь
нет и не будет: агенту нечего в неё положить. Блок «Источники:\n- none» в
конце каждого ответа — не RAG-механизм, а осознанно оставленное поле для
единообразия формата ответа с epoz (осталось от прежней реализации).

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
│                                    частью после "chatcmpl-"/"resp_" в id ответа
├── user_id            UUID          NOT NULL — владелец записи
├── conversation_id    UUID          nullable, FK → conversations.id (ON DELETE CASCADE) —
│                                    в форме Chat Completions только привязка к чату
│                                    в UI-списке; в форме Responses ЕЩЁ И источник
│                                    истории для генерации (см. POST /v1/responses)
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
модели оказался пустым. Одинаково для обеих форм.

## Модель

Настраивается через `.env`:
```
OLLAMA_MODEL=qwen3.6:35b
```
Системный промпт (`system_prompt`, по умолчанию — универсальный ассистент
на русском) и `history_limit` (по умолчанию `10`) — настройки в
`app/config.py`, не в `.env`. `history_limit` — сколько последних сообщений
чата подтягивать из БД в форме Responses при переданном `conversation_id`
(на форму Chat Completions не влияет — там истории из БД не бывает вообще).

## API

Все эндпоинты требуют заголовок `X-User-Id: <uuid>`. Ресурсы скоупятся по этому идентификатору; обращение к чужому ресурсу возвращает `404`.

### `POST /v1/chat/completions`

Генерация ответа, форма Chat Completions. Клиент присылает **всю** историю диалога в `messages[]` — сервис её не хранит и не переиспользует между запросами, независимо от `conversation_id`. Сервис добавляет системный промпт (`settings.system_prompt`) первым сообщением перед отправкой в Ollama — это происходит на сервере и не настраивается через запрос.

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
| `conversation_id` | UUID string | нет | платформенное расширение — привязать сообщение к чату из `/v1/platform/conversations`. **Только ярлык для UI** в этой форме — контекст всегда из `messages[]`. Чужой/несуществующий `conversation_id` → `404` |

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
`conversation_id` в ответе — `null`, если не был передан в запросе. `id` (`chatcmpl-<uuid>`) — ключ для фидбэка и повторного чтения ниже. Если модель вернула пустой ответ — сообщение ассистента не сохраняется, но клиент всё равно получает валидный `id` в ответе.

Если клиент обрывает соединение до завершения генерации, уже полученная часть ответа всё равно сохраняется в БД (без блока «Источники»).

---

### `GET /v1/chat/completions/{completion_id}`

Получить ранее сгенерированный ответ повторно (в форме `chat.completion`) — по `id`, который пришёл в ответе. Работает для сообщений, сгенерированных **любой** из двух форм: `chatcmpl-<uuid>`, `resp_<uuid>` или голый UUID.

`404`, если `id` не найден, принадлежит не вам, либо указывает на сообщение с `role: "user"`.

---

### `POST /v1/responses`

Генерация ответа, форма Responses API. Два режима в зависимости от того, передан ли `conversation_id`:

- **Без `conversation_id`** — stateless: `input` должен содержать всё, что нужно модели (аналог `messages[]`).
- **С `conversation_id`** — `input` должен содержать **только новый ход**. Сервис сам читает последние `history_limit` сообщений этого чата из БД (`app/config.py`, по умолчанию 10) и подставляет их как историю. Системный промпт добавляется так же, как в форме Chat Completions. Если клиент всё равно пришлёт историю в `input` вместе с `conversation_id` — `422`:
  ```json
  {"error": {"message": "При переданном conversation_id input должен содержать только новый ход (без истории) — история собирается агентом из БД по conversation_id", ...}}
  ```

Тело запроса (`input` строкой):
```json
{
  "model": "chat",
  "input": "привет",
  "stream": true,
  "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```
Тело запроса (`input` списком items — без `conversation_id`, история целиком от клиента):
```json
{
  "model": "chat",
  "input": [
    {"role": "user", "content": "привет"},
    {"role": "assistant", "content": [{"type": "output_text", "text": "привет! чем помочь?"}]},
    {"role": "user", "content": "расскажи анекдот"}
  ]
}
```
Части `content`-массива: `"input_text"` — новый ввод, `"output_text"` — эхо прошлого ответа модели (стандартный способ вести историю у Responses API вручную).

**Нестрим-ответ**:
```json
{
  "id": "resp_1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "object": "response",
  "created_at": 1735900000,
  "status": "completed",
  "model": "chat",
  "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "output": [{
    "id": "msg_1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
    "type": "message",
    "status": "completed",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "Заходит как-то...\n\nИсточники:\n- none", "annotations": []}]
  }],
  "usage": {"input_tokens": 128, "output_tokens": 42, "total_tokens": 170}
}
```
`usage` здесь — `input_tokens`/`output_tokens` (терминология Responses API), а не `prompt_tokens`/`completion_tokens`.

**Стрим-ответ** — гранулярные типизированные SSE-события, не единый тип чанка:
```
event: response.created
data: {"type":"response.created","sequence_number":1,"response":{"id":"resp_...","status":"in_progress",...}}

event: response.output_item.added
data: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{...}}

event: response.content_part.added
data: {"type":"response.content_part.added","sequence_number":3,...}

event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":4,"item_id":"msg_...","delta":"Заходит"}

... (ещё response.output_text.delta на каждый токен) ...

event: response.output_text.done
data: {...}

event: response.content_part.done
data: {...}

event: response.output_item.done
data: {...}

event: response.completed
data: {"type":"response.completed","sequence_number":N,"response":{"id":"resp_...","status":"completed","output":[...],"usage":{...}}}
```
`sequence_number` — сквозной монотонный счётчик на весь поток. Ошибка в процессе генерации — `event: error` с `sequence_number: 9999` (зарезервирован под ошибку).

Если клиент обрывает соединение до завершения — частичный ответ сохраняется, как и в форме Chat Completions.

---

### `GET /v1/responses/{completion_id}`

То же самое, что `GET /v1/chat/completions/{completion_id}`, но возвращает `response`-объект. Принимает id в любом виде.

---

### `POST/GET/DELETE /v1/chat/completions/{completion_id}/feedback`

Произвольный JSON-фидбэк к сообщению ассистента — **общий путь для обеих форм генерации**, `{completion_id}` принимает `chatcmpl-<uuid>`, `resp_<uuid>` или голый UUID одинаково. Формат `payload` не зафиксирован схемой. **Повторный `POST` мёржит** новый `payload` с уже сохранённым на уровне JSONB-ключей (`payload || new_payload`), а не перезаписывает целиком.

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

Платформенное расширение для UI (список чатов, история, переименование, удаление). **Не входит ни в одну спеку OpenAI** и не участвует в генерации напрямую — см. «API — общая идея» выше. Форма ручек идентична epoz.

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
История сообщений чата вместе с фидбэком.
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
`id` ассистентского сообщения — тот же UUID, что использовать для `/v1/chat/completions/{id}/feedback` и повторного чтения в любой из форм.

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

# ============ форма Chat Completions ============

curl -X POST $BASE/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "chat", "messages": [{"role": "user", "content": "привет"}]}'

curl -N -X POST $BASE/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "chat", "stream": true, "messages": [{"role": "user", "content": "расскажи анекдот"}]}'

# ============ форма Responses ============

# без conversation_id — input строкой
curl -N -X POST $BASE/v1/responses \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "chat", "stream": true, "input": "расскажи анекдот"}'

# создать чат, затем спросить — история дальше ведётся сервером
curl -X POST $BASE/v1/platform/conversations \
  -H "X-User-Id: $U" -H "Content-Type: application/json" -d '{"title": "Тестовый чат"}'

CID=3fa85f64-5717-4562-b3fc-2c963f66afa6

curl -X POST $BASE/v1/responses \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d "{\"model\": \"chat\", \"conversation_id\": \"$CID\", \"input\": \"привет\"}"

# следующий вопрос в том же чате — снова только новый ход
curl -X POST $BASE/v1/responses \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d "{\"model\": \"chat\", \"conversation_id\": \"$CID\", \"input\": \"расскажи анекдот\"}"

ID=resp_1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2

curl $BASE/v1/responses/$ID -H "X-User-Id: $U"

# ============ общее для обеих форм ============

# получить ответ повторно как chat.completion, независимо от формы генерации
curl $BASE/v1/chat/completions/$ID -H "X-User-Id: $U"

curl -X POST $BASE/v1/chat/completions/$ID/feedback \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"vote": 1, "comment": "Хороший ответ"}'
curl $BASE/v1/chat/completions/$ID/feedback -H "X-User-Id: $U"
curl -X DELETE $BASE/v1/chat/completions/$ID/feedback -H "X-User-Id: $U"

curl $BASE/v1/platform/conversations -H "X-User-Id: $U"
curl $BASE/v1/platform/conversations/$CID/messages -H "X-User-Id: $U"

curl -X PATCH $BASE/v1/platform/conversations/$CID \
  -H "X-User-Id: $U" -H "Content-Type: application/json" -d '{"title": "Новое название"}'
curl -X DELETE $BASE/v1/platform/conversations/$CID -H "X-User-Id: $U"

# ============ ошибки ============

# conversation_id + история в input одновременно (форма Responses) -> 422
curl -X POST $BASE/v1/responses \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d "{\"model\": \"chat\", \"conversation_id\": \"$CID\", \"input\": [
        {\"role\": \"user\", \"content\": \"вопрос 1\"},
        {\"role\": \"assistant\", \"content\": \"ответ 1\"},
        {\"role\": \"user\", \"content\": \"вопрос 2\"}
      ]}"

# --- служебное ---
curl $BASE/healthz
```