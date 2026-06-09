# Инструкция для ИИ: деплой Python Telegram-бота на HuggingFace бесплатно

Ты — ИИ-ассистент. Твоя задача — помочь задеплоить Python Telegram-бота на HuggingFace Spaces бесплатно.
Следуй шагам по порядку. На каждом шаге говори пользователю что именно нужно сделать и жди подтверждения перед переходом к следующему шагу.

## Контекст

HuggingFace Spaces (бесплатный тариф) блокирует прямые исходящие соединения к api.telegram.org.
Решение: использовать Cloudflare Worker как прокси между ботом и Telegram API.
Бот слушает входящие webhook-запросы от Telegram на порту 7860.
Чтобы HuggingFace не засыпал — настраивается внешний пингователь через cron-job.org.
Все сервисы бесплатны, карта не нужна.

## Сервисы

- HuggingFace (huggingface.co) — хостинг бота
- Cloudflare Workers (cloudflare.com) — прокси к Telegram API, 100 000 запросов в день бесплатно
- cron-job.org (console.cron-job.org) — пингует сервер каждые 5 минут чтобы не засыпал
- GitHub (github.com) — хранение кода

## Шаг 1. Изменить код бота

Попроси пользователя прислать файл main.py и requirements.txt.

### 1.1 Добавить в начало main.py (после всех импортов)

```python
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

_telegram_api_url = os.getenv('TELEGRAM_API_URL')
if _telegram_api_url:
    telebot.apihelper.API_URL = _telegram_api_url
```

### 1.2 Заменить bot.infinity_polling() на следующий блок

```python
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

class _BotHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        logger.info("Пинг получен")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            update = telebot.types.Update.de_json(json.loads(body.decode('utf-8')))
            bot.process_new_updates([update])
        except Exception as e:
            logger.error(f"Webhook update error: {e}")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

if WEBHOOK_URL:
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info("Webhook установлен")
    except Exception as e:
        logger.error(f"Ошибка установки webhook: {e}")
    HTTPServer(("0.0.0.0", 7860), _BotHandler).serve_forever()
else:
    try:
        bot.remove_webhook()
    except:
        pass
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
```

### 1.3 Создать Dockerfile в корне проекта

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

EXPOSE 7860

CMD ["python", "main.py"]
```

После внесения изменений — запушить всё на GitHub.

## Шаг 2. Настроить Cloudflare Worker

Сказать пользователю:
1. Зайти на cloudflare.com, зарегистрироваться (только email)
2. Workers & Pages → Create → Start with Hello World
3. Вставить код и нажать Deploy:

```javascript
export default {
  async fetch(request) {
    const url = new URL(request.url);
    const telegramUrl = "https://api.telegram.org" + url.pathname + url.search;
    return fetch(new Request(telegramUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body,
    }));
  }
};
```

4. Скопировать URL воркера (формат: https://xxx.workers.dev)

Спросить пользователя: "Пришли URL воркера"

## Шаг 3. Создать HuggingFace Space

Сказать пользователю:
1. Зайти на huggingface.co, зарегистрироваться
2. New Space → Docker → Public
3. Загрузить файлы: все .py файлы, Dockerfile, requirements.txt
4. Открыть Space в новой вкладке и скопировать URL

Формат URL: https://{username}-{spacename}.hf.space (строчные буквы, подчёркивания заменяются дефисами)

Спросить пользователя: "Пришли URL твоего Space"

## Шаг 4. Добавить переменные окружения

Сказать пользователю перейти в Settings → Variables and secrets и добавить:

- BOT_TOKEN = токен от @BotFather
- WEBHOOK_URL = URL Space (https://username-spacename.hf.space)
- TELEGRAM_API_URL = URL воркера + /bot{0}/{1} (например: https://xxx.workers.dev/bot{0}/{1})
- плюс все остальные переменные проекта

## Шаг 5. Настроить пингователь

Сказать пользователю:
1. Зайти на console.cron-job.org, зарегистрироваться
2. Create cronjob
3. URL: URL Space
4. Schedule: каждые 5 минут
5. Save

## Шаг 6. Проверка

Попросить пользователя прислать логи из HuggingFace Space (раздел Logs).

Успешный деплой выглядит так:
```
Webhook режим: https://...
Webhook установлен
Пинг получен
```

## Диагностика ошибок

Read timed out при старте
- Причина: HuggingFace блокирует api.telegram.org
- Решение: проверить что TELEGRAM_API_URL задан в формате https://xxx.workers.dev/bot{0}/{1}

Бот не отвечает на сообщения
- Причина: неверный WEBHOOK_URL
- Решение: открыть Space в новой вкладке, скопировать реальный URL, обновить переменную

Invalid JWT Signature
- Причина: файл credentials.json повреждён при загрузке
- Решение: пересоздать ключ в Google Cloud Console → IAM & Admin → Service Accounts → Keys → Add Key → JSON

Space засыпает
- Причина: нет пингователя
- Решение: настроить cron-job.org каждые 5 минут

Webhook не устанавливается
- Причина: TELEGRAM_API_URL указан неверно
- Решение: проверить формат — фигурные скобки {0} и {1} должны быть буквально в строке

## Итог

После всех шагов бот работает 24/7 на полностью бесплатной инфраструктуре.
Cloudflare Worker можно переиспользовать для всех последующих проектов — менять нужно только переменные в настройках каждого нового Space.
