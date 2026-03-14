import os
import logging
import asyncio
import aiohttp
import secrets
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token="8397283414:AAER0owctNVCN_ZwEMPO6iT5C2IjGYmluAw")  # Замените на ваш токен
dp = Dispatcher()

# Конфигурация платежей
PROVIDER_TOKEN = "YOUR_PROVIDER_TOKEN"  # Токен Telegram Stars
CRYPTO_PAY_TOKEN = "462970:AAC4owYb7jgRvU5heLKzcKPUZNGQIY4u1ou"  # Токен Crypto Pay (получить у @CryptoBot)
CRYPTO_PAY_API = "https://pay.crypt.bot/api/"

# Комиссии
COMMISSION_STARS = 0.02  # 2%
COMMISSION_TON = 0.05    # 5%

# Временное хранилище данных
deals = {}
users = {}
complaints = {}
moderator_ids = [ 7782467381]   # ID модераторов
support_ids = [ 7782467381]     # ID поддержки
pending_withdrawals = {}   # вывод звёзд
ton_invoices = {}          # счета TON: invoice_id -> {'user_id':, 'amount':, 'status':}

# Классы состояний
class Form(StatesGroup):
    feedback_text = State()
    complaint_description = State()
    complaint_proof = State()

class DealCreation(StatesGroup):
    waiting_for_username = State()
    waiting_for_gift_link = State()
    waiting_for_price = State()
    waiting_for_currency = State()  # stars или ton

# Вспомогательные функции для работы с Crypto Pay API
async def create_crypto_invoice(amount_ton: float, description: str, payload: str) -> dict:
    """Создаёт счёт в TON через Crypto Pay и возвращает данные счёта."""
    url = f"{CRYPTO_PAY_API}createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    data = {
        "asset": "TON",
        "amount": str(amount_ton),
        "description": description,
        "payload": payload,
        "paid_btn_name": "callback",
        "paid_btn_url": "https://t.me/your_bot?start=payment_success"  # можно использовать глубокую ссылку
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=data) as resp:
            if resp.status == 200:
                result = await resp.json()
                if result.get("ok"):
                    return result["result"]
    return None

async def get_crypto_invoice_status(invoice_id: int) -> str:
    """Проверяет статус счёта (active, paid, expired)."""
    url = f"{CRYPTO_PAY_API}getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    params = {"invoice_ids": str(invoice_id)}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                result = await resp.json()
                if result.get("ok") and result["result"]["items"]:
                    return result["result"]["items"][0]["status"]
    return "unknown"

# Фоновая задача для проверки статусов счетов TON (только для пополнения баланса)
async def check_ton_invoices():
    while True:
        await asyncio.sleep(30)
        for inv_id, inv_data in list(ton_invoices.items()):
            if inv_data["status"] == "active":
                status = await get_crypto_invoice_status(inv_id)
                if status == "paid":
                    inv_data["status"] = "paid"
                    user_id = inv_data["user_id"]
                    amount_ton = inv_data["amount"]
                    # Зачисляем средства пользователю
                    if user_id not in users:
                        users[user_id] = {
                            "stars_balance": 0,
                            "frozen_stars": 0,
                            "ton_balance": 0,
                            "frozen_ton": 0,
                            "rating": 4.8,
                            "reviews_count": 0,
                            "deals_completed": 0,
                            "username": (await bot.get_chat(user_id)).username or f"user_{user_id}",
                        }
                    users[user_id]["ton_balance"] = users[user_id].get("ton_balance", 0) + amount_ton
                    await bot.send_message(
                        user_id,
                        f"✅ Пополнение TON прошло успешно! На ваш баланс зачислено {amount_ton} TON."
                    )
                elif status == "expired":
                    inv_data["status"] = "expired"
                    await bot.send_message(
                        inv_data["user_id"],
                        f"❌ Счёт TON на {inv_data['amount']} TON истёк. Попробуйте снова."
                    )

# Команда /donate для звёзд
@dp.message(Command("donate"))
async def cmd_donate(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Используйте формат: /donate <сумма в звёздах>")
            return
        amount = int(args[1])
        if amount < 1:
            await message.answer("Минимальная сумма donation - 1 звезда!")
            return
        prices = [LabeledPrice(label="Пополнение звёздами", amount=amount)]
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="Пополнение баланса звёздами",
            description=f"Пополнение на {amount} звёзд",
            payload=f"donation_{message.from_user.id}",
            provider_token=PROVIDER_TOKEN,
            currency="XTR",
            prices=prices,
            start_parameter="donation",
            need_email=False,
            need_phone_number=False,
            need_shipping_address=False,
            is_flexible=False,
            max_tip_amount=0
        )
    except (IndexError, ValueError):
        await message.answer("Используйте формат: /donate <сумма>")
    except Exception as e:
        await message.answer(f"Ошибка создания счета: {str(e)}")
        logging.error(f"Error creating invoice: {e}")

# Команда /donate_ton для TON (пополнение баланса)
@dp.message(Command("donate_ton"))
async def cmd_donate_ton(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Используйте формат: /donate_ton <сумма в TON>")
            return
        amount = float(args[1])
        if amount <= 0:
            await message.answer("Сумма должна быть положительной")
            return
        # Создаём счёт в Crypto Pay
        payload = f"ton_donation_{message.from_user.id}_{secrets.token_hex(4)}"
        invoice = await create_crypto_invoice(amount, "Пополнение баланса TON", payload)
        if invoice:
            ton_invoices[invoice["invoice_id"]] = {
                "user_id": message.from_user.id,
                "amount": amount,
                "status": "active"
            }
            await message.answer(
                f"💰 Счёт на {amount} TON создан.\n"
                f"Ссылка для оплаты: {invoice['pay_url']}\n\n"
                f"После оплаты баланс будет пополнен автоматически."
            )
        else:
            await message.answer("Ошибка создания счёта. Попробуйте позже.")
    except ValueError:
        await message.answer("Введите корректную сумму.")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
        logging.error(f"Error in donate_ton: {e}")

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    amount = message.successful_payment.total_amount
    payload = message.successful_payment.invoice_payload

    if payload.startswith("donation_"):
        if user_id not in users:
            users[user_id] = {
                "stars_balance": 0,
                "frozen_stars": 0,
                "ton_balance": 0,
                "frozen_ton": 0,
                "rating": 4.8,
                "reviews_count": 0,
                "deals_completed": 0,
                "username": message.from_user.username or f"user_{user_id}",
            }
        users[user_id]["stars_balance"] += amount
        await message.answer(
            f"✅ Оплата прошла успешно! На ваш баланс зачислено {amount} звёзд\n"
            f"💰 Текущий баланс: {users[user_id]['stars_balance']} звёзд\n"
            f"📋 ID транзакции: {message.successful_payment.telegram_payment_charge_id}"
        )

# Команда /get для вывода звезд
@dp.message(Command("get"))
async def cmd_get(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Используйте формат: /get <сумма>")
            return
        amount = int(args[1])
        user_id = message.from_user.id
        if user_id not in users or users[user_id].get("stars_balance", 0) < amount:
            await message.answer("❌ Недостаточно звезд для вывода")
            return
        available_balance = users[user_id]["stars_balance"] - users[user_id].get("frozen_stars", 0)
        if available_balance < amount:
            await message.answer("❌ Недостаточно доступных звезд. Часть средств заморожена.")
            return
        if user_id not in pending_withdrawals:
            pending_withdrawals[user_id] = []
        withdrawal_id = len(pending_withdrawals[user_id]) + 1
        pending_withdrawals[user_id].append({
            "id": withdrawal_id,
            "amount": amount,
            "timestamp": datetime.now(),
            "status": "pending"
        })
        users[user_id]["stars_balance"] -= amount
        await message.answer(
            f"✅ Запрос на вывод {amount} звезд принят.\n"
            f"Средства будут доступны для вывода через 7 дней.\n"
            f"ID запроса: {withdrawal_id}"
        )
    except (IndexError, ValueError):
        await message.answer("Используйте формат: /get <сумма>")
    except Exception as e:
        await message.answer(f"Ошибка вывода: {str(e)}")
        logging.error(f"Error processing withdrawal: {e}")

# Команда /testbalance (только для модераторов)
@dp.message(Command("testbalance"))
async def cmd_test_balance(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 3:
            await message.answer("Используйте формат: /testbalance <валюта> <сумма>\nПример: /testbalance stars 100")
            return
        currency = args[1].lower()
        amount = float(args[2])
        if amount < 0:
            await message.answer("❌ Сумма должна быть положительной")
            return
        user_id = message.from_user.id
        if user_id not in users:
            users[user_id] = {
                "stars_balance": 0,
                "frozen_stars": 0,
                "ton_balance": 0,
                "frozen_ton": 0,
                "rating": 4.8,
                "reviews_count": 0,
                "deals_completed": 0,
                "username": message.from_user.username or f"user_{user_id}",
            }
        if currency == "stars":
            users[user_id]["stars_balance"] += amount
            await message.answer(f"✅ Баланс звёзд пополнен на {amount}.\n💰 Текущий баланс: {users[user_id]['stars_balance']} звёзд")
        elif currency == "ton":
            users[user_id]["ton_balance"] += amount
            await message.answer(f"✅ Баланс TON пополнен на {amount}.\n💰 Текущий баланс: {users[user_id]['ton_balance']} TON")
        else:
            await message.answer("❌ Неверная валюта. Используйте stars или ton.")
    except (IndexError, ValueError):
        await message.answer("❌ Неверный формат. Используйте: /testbalance <валюта> <число>")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logging.error(f"Error in testbalance: {e}")

# Стартовая команда (обычная и с deep link)
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("deal_"):
        token = args[1][5:]
        deal = next((d for d in deals.values() if d.get("token") == token), None)
        if not deal:
            await message.answer("Сделка не найдена или устарела.")
            return
        if message.from_user.username != deal['seller_username']:
            await message.answer("Эта ссылка предназначена для другого пользователя.")
            return
        deal['seller_id'] = message.from_user.id
        deal['status'] = "pending_acceptance"
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_deal_{deal['id']}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"reject_deal_{deal['id']}"),
            InlineKeyboardButton(text="⚠️ Оспорить", callback_data=f"dispute_{deal['id']}")
        )
        currency_display = "звёзд" if deal['currency'] == 'stars' else "TON"
        await message.answer(
            f"Вам предлагают купить ваш подарок:\n\n"
            f"Покупатель: @{deal['buyer_username']}\n"
            f"Подарок: {deal['gift_link']}\n"
            f"Цена: {deal['price']} {currency_display}\n\n"
            f"Если вы согласны продать, нажмите «Принять». Покупатель сможет оплатить.",
            reply_markup=keyboard.as_markup()
        )
    else:
        # Обычный старт
        user_id = message.from_user.id
        if user_id not in users:
            users[user_id] = {
                "stars_balance": 0,
                "frozen_stars": 0,
                "ton_balance": 0,
                "frozen_ton": 0,
                "rating": 4.8,
                "reviews_count": 0,
                "deals_completed": 0,
                "username": message.from_user.username or f"user_{user_id}",
            }
        keyboard = InlineKeyboardBuilder()
        buttons = [
            InlineKeyboardButton(text="➕ Создать сделку", callback_data="create_deal"),
            InlineKeyboardButton(text="📋 Мои сделки", callback_data="my_deals"),
            InlineKeyboardButton(text="💰 Баланс", callback_data="check_balance"),
            InlineKeyboardButton(text="❓ Поддержка", callback_data="support"),
            InlineKeyboardButton(text="👥 Группы бота", callback_data="groups"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")
        ]
        keyboard.add(*buttons)
        keyboard.adjust(2, 2, 2)
        await message.answer(
            "Добро пожаловать! Здесь вы можете безопасно совершать сделки с NFT и подарками.",
            reply_markup=keyboard.as_markup()
        )

# Создание сделки (покупатель)
@dp.callback_query(F.data == "create_deal")
async def create_deal_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите username продавца (без @):")
    await state.set_state(DealCreation.waiting_for_username)

@dp.message(DealCreation.waiting_for_username)
async def process_username(message: types.Message, state: FSMContext):
    username = message.text.strip().replace("@", "")
    await state.update_data(target_username=username)
    await message.answer("Теперь отправьте ссылку на подарок, который хотите купить:")
    await state.set_state(DealCreation.waiting_for_gift_link)

@dp.message(DealCreation.waiting_for_gift_link)
async def process_gift_link(message: types.Message, state: FSMContext):
    gift_link = message.text.strip()
    await state.update_data(gift_link=gift_link)
    await message.answer("Введите цену, которую готовы заплатить:")
    await state.set_state(DealCreation.waiting_for_price)

@dp.message(DealCreation.waiting_for_price)
async def process_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        if price <= 0:
            raise ValueError
        await state.update_data(price=price)
        # Предлагаем выбор валюты
        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(text="⭐ Звёзды", callback_data="currency_stars"))
        keyboard.add(InlineKeyboardButton(text="💎 TON", callback_data="currency_ton"))
        await message.answer("Выберите валюту сделки:", reply_markup=keyboard.as_markup())
        await state.set_state(DealCreation.waiting_for_currency)
    except ValueError:
        await message.answer("Введите корректное положительное число.")

@dp.callback_query(DealCreation.waiting_for_currency, F.data.in_({"currency_stars", "currency_ton"}))
async def process_currency(callback: types.CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1]  # stars или ton
    await state.update_data(currency=currency)
    data = await state.get_data()
    target_username = data['target_username']
    gift_link = data['gift_link']
    price = data['price']
    buyer_id = callback.from_user.id
    buyer_username = users[buyer_id]['username']

    deal_token = secrets.token_urlsafe(16)
    deal_id = len(deals) + 1

    deals[deal_id] = {
        "id": deal_id,
        "buyer_id": buyer_id,
        "buyer_username": buyer_username,
        "seller_username": target_username,
        "gift_link": gift_link,
        "price": price,
        "currency": currency,
        "status": "pending_moderator",
        "token": deal_token,
        "created_at": datetime.now()
    }

    bot_username = (await bot.get_me()).username
    deal_link = f"https://t.me/{bot_username}?start=deal_{deal_token}"

    # === ОТПРАВКА УВЕДОМЛЕНИЙ МОДЕРАТОРАМ И ПРОДАВЦУ ===
    currency_display = "звёзд" if currency == "stars" else "TON"  # <-- 4 пробела

# 1. Отправка модераторам (как и было)
    for mod_id in moderator_ids:  # <-- 4 пробела
        await bot.send_message(  # <-- 8 пробелов
        mod_id,
        f"🔔 Новая сделка #{deal_id}\n"
        f"Покупатель: @{buyer_username}\n"
        f"Продавец: @{target_username}\n"
        f"Ссылка на подарок: {gift_link}\n"
        f"Цена: {price} {currency_display}\n\n"
        f"📎 Ссылка для продавца:\n{deal_link}"
    )

# 2. Отправка уведомления напрямую продавцу (НОВОЕ)
try:  # <-- 4 пробела
    # Пытаемся найти ID продавца по его username в базе users
    seller_id = None  # <-- 8 пробелов
    for uid, udata in users.items():  # <-- 8 пробелов
        if udata.get('username') == target_username:  # <-- 12 пробелов
            seller_id = uid  # <-- 16 пробелов
            break

    if seller_id:
        # Если продавец уже есть в базе (запускал бота) - отправляем ему лично
        await bot.send_message(  # <-- 12 пробелов
            seller_id,
            f"🔔 Вам предложение купить ваш подарок!\n\n"
            f"Покупатель: @{buyer_username}\n"
            f"Подарок: {gift_link}\n"
            f"Цена: {price} {currency_display}\n\n"
            f"📎 Ваша персональная ссылка для принятия сделки:\n{deal_link}\n\n"
            f"❗️ Если вы согласны, нажмите Start в боте и перейдите по ссылке выше."
        )
        logger.info(f"Уведомление отправлено продавцу {target_username} (ID: {seller_id})")
    else:
        # Если продавца нет в базе - отправляем ссылку покупателю, чтобы тот передал
        await bot.send_message(  # <-- 12 пробелов
            callback.from_user.id,
            f"⚠️ Продавец @{target_username} ещё не запускал бота, поэтому я не могу отправить ему уведомление автоматически.\n\n"
            f"📎 Пожалуйста, отправьте ему эту ссылку самостоятельно:\n{deal_link}"
        )
        logger.warning(f"Продавец {target_username} не найден в базе, ссылка отправлена покупателю.")

except Exception as e:
    logger.error(f"Ошибка при отправке уведомления продавцу {target_username}: {e}")
    # Запасной вариант - отправляем ссылку покупателю
    await bot.send_message(  # <-- 8 пробелов
        callback.from_user.id,
        f"⚠️ Произошла техническая ошибка при уведомлении продавца.\n\n"
        f"📎 Пожалуйста, отправьте ему эту ссылку самостоятельно:\n{deal_link}"
    )

# Сообщение покупателю о создании сделки
await callback.message.answer(  # <-- 4 пробела
    f"✅ Сделка создана. Статус сделки можно отслеживать в разделе «Мои сделки»."
)
await state.clear()  # <-- 4 пробела
# === КОНЕЦ БЛОКА УВЕДОМЛЕНИЙ ===
    
# Принятие сделки продавцом
@dp.callback_query(F.data.startswith("accept_deal_"))
async def accept_deal(callback: types.CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    deal = deals.get(deal_id)
    if not deal or deal['status'] != 'pending_acceptance':
        await callback.message.answer("Сделка уже обработана.")
        return
    if callback.from_user.id != deal['seller_id']:
        await callback.message.answer("Это не ваша сделка.")
        return

    deal['status'] = 'accepted_by_seller'
    currency = deal['currency']

    # Для обеих валют теперь используем внутренний баланс
    keyboard = InlineKeyboardBuilder()
    if currency == 'stars':
        keyboard.add(InlineKeyboardButton(text="💳 Оплатить звёздами", callback_data=f"pay_deal_{deal_id}"))
    else:  # TON
        keyboard.add(InlineKeyboardButton(text="💳 Оплатить TON", callback_data=f"pay_deal_{deal_id}"))
    keyboard.add(InlineKeyboardButton(text="⚠️ Оспорить", callback_data=f"dispute_{deal_id}"))

    await bot.send_message(
        deal['buyer_id'],
        f"✅ Продавец @{deal['seller_username']} принял вашу сделку.\n"
        f"Сумма к оплате: {deal['price']} {'звёзд' if currency == 'stars' else 'TON'}.\n"
        f"Нажмите «Оплатить», чтобы завершить сделку.",
        reply_markup=keyboard.as_markup()
    )

    await callback.message.answer("Вы приняли сделку. Покупатель получил уведомление об оплате.")

# Отказ от сделки продавцом
@dp.callback_query(F.data.startswith("reject_deal_"))
async def reject_deal(callback: types.CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    deal = deals.get(deal_id)
    if not deal or deal['status'] != 'pending_acceptance':
        await callback.message.answer("Сделка уже обработана.")
        return
    if callback.from_user.id != deal['seller_id']:
        await callback.message.answer("Это не ваша сделка.")
        return
    deal['status'] = 'rejected'
    await callback.message.answer("Вы отказались от сделки.")
    await bot.send_message(
        deal['buyer_id'],
        f"Продавец @{deal['seller_username']} отклонил вашу сделку."
    )

# Оплата сделки покупателем (для обеих валют с баланса)
@dp.callback_query(F.data.startswith("pay_deal_"))
async def pay_deal(callback: types.CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    deal = deals.get(deal_id)
    if not deal or deal['status'] != 'accepted_by_seller':
        await callback.message.answer("Сделка уже оплачена или недоступна.")
        return
    if callback.from_user.id != deal['buyer_id']:
        await callback.message.answer("Это не ваша сделка.")
        return

    buyer_id = deal['buyer_id']
    price = deal['price']
    currency = deal['currency']

    if currency == 'stars':
        if users.get(buyer_id, {}).get('stars_balance', 0) < price:
            await callback.message.answer(f"❌ Недостаточно звёзд. Нужно {price}. Пополните баланс через /donate")
            return
        # Замораживаем средства покупателя
        users[buyer_id]['stars_balance'] -= price
        users[buyer_id]['frozen_stars'] = users[buyer_id].get('frozen_stars', 0) + price
    else:  # TON
        if users.get(buyer_id, {}).get('ton_balance', 0) < price:
            await callback.message.answer(f"❌ Недостаточно TON. Нужно {price:.2f}. Пополните баланс через /donate_ton")
            return
        # Замораживаем средства покупателя
        users[buyer_id]['ton_balance'] -= price
        users[buyer_id]['frozen_ton'] = users[buyer_id].get('frozen_ton', 0) + price

    deal['status'] = 'payment_received'

    # Уведомляем продавца
    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="📤 Я отправил подарок", callback_data=f"gift_sent_{deal_id}"))
    keyboard.add(InlineKeyboardButton(text="⚠️ Оспорить", callback_data=f"dispute_{deal_id}"))
    currency_display = "звёзд" if currency == 'stars' else "TON"
    await bot.send_message(
        deal['seller_id'],
        f"💰 Покупатель @{deal['buyer_username']} оплатил сделку #{deal_id} ({price} {currency_display}).\n"
        f"Теперь отправьте подарок и нажмите кнопку подтверждения.",
        reply_markup=keyboard.as_markup()
    )
    await callback.message.answer(f"✅ Оплата прошла. Ожидайте отправки подарка от продавца.")

# Продавец отправил подарок
@dp.callback_query(F.data.startswith("gift_sent_"))
async def gift_sent(callback: types.CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    deal = deals.get(deal_id)
    if not deal or deal['status'] != 'payment_received':
        await callback.message.answer("Неверный статус сделки.")
        return
    if callback.from_user.id != deal['seller_id']:
        await callback.message.answer("Это не ваша сделка.")
        return

    deal['status'] = 'gift_sent'

    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="✅ Подтвердить получение", callback_data=f"confirm_receipt_{deal_id}"))
    keyboard.add(InlineKeyboardButton(text="⚠️ Оспорить", callback_data=f"dispute_{deal_id}"))
    await bot.send_message(
        deal['buyer_id'],
        f"Продавец @{deal['seller_username']} отправил вам подарок.\n"
        f"Если вы получили его, нажмите кнопку подтверждения.",
        reply_markup=keyboard.as_markup()
    )
    await callback.message.answer("Уведомление отправлено покупателю.")

# Покупатель подтвердил получение
@dp.callback_query(F.data.startswith("confirm_receipt_"))
async def confirm_receipt(callback: types.CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    deal = deals.get(deal_id)
    if not deal or deal['status'] != 'gift_sent':
        await callback.message.answer("Неверный статус сделки.")
        return
    if callback.from_user.id != deal['buyer_id']:
        await callback.message.answer("Это не ваша сделка.")
        return

    buyer_id = deal['buyer_id']
    seller_id = deal['seller_id']
    price = deal['price']
    currency = deal['currency']

    if currency == 'stars':
        # Размораживаем средства у покупателя
        users[buyer_id]['frozen_stars'] -= price
        # Зачисляем продавцу с учётом комиссии
        commission = COMMISSION_STARS
        seller_amount = price * (1 - commission)
        users[seller_id]['stars_balance'] += seller_amount
        users[seller_id]['frozen_stars'] = users[seller_id].get('frozen_stars', 0) + seller_amount  # заморозка на 7 дней
    else:  # TON
        users[buyer_id]['frozen_ton'] -= price
        commission = COMMISSION_TON
        seller_amount = price * (1 - commission)
        users[seller_id]['ton_balance'] += seller_amount
        users[seller_id]['frozen_ton'] = users[seller_id].get('frozen_ton', 0) + seller_amount

    deal['seller_freeze_until'] = datetime.now() + timedelta(days=7)
    deal['status'] = 'completed'

    # Счётчики сделок
    users[seller_id]['deals_completed'] = users[seller_id].get('deals_completed', 0) + 1
    users[buyer_id]['deals_completed'] = users[buyer_id].get('deals_completed', 0) + 1

    currency_display = "звёзд" if currency == "stars" else "TON"
    await callback.message.answer(f"✅ Сделка успешно завершена! Спасибо.")
    await bot.send_message(
        seller_id,
        f"✅ Покупатель подтвердил получение. Средства ({seller_amount:.2f} {currency_display}) зачислены и будут доступны через 7 дней."
    )

# Оспаривание сделки
@dp.callback_query(F.data.startswith("dispute_"))
async def dispute_deal(callback: types.CallbackQuery, state: FSMContext):
    deal_id = int(callback.data.split("_")[1])
    deal = deals.get(deal_id)
    if not deal:
        await callback.message.answer("Сделка не найдена.")
        return
    if callback.from_user.id not in (deal['buyer_id'], deal['seller_id']):
        await callback.message.answer("Это не ваша сделка.")
        return
    await state.update_data(deal_id=deal_id)
    await callback.message.answer("📝 Опишите причину спора (проблему с отправкой/получением подарка):")
    await state.set_state(Form.complaint_description)

@dp.message(Form.complaint_description)
async def handle_complaint_description(message: types.Message, state: FSMContext):
    data = await state.get_data()
    deal_id = data['deal_id']
    deal = deals.get(deal_id)
    if not deal:
        await message.answer("❌ Сделка не найдена")
        await state.clear()
        return

    complaint_id = len(complaints) + 1
    complaints[complaint_id] = {
        "deal_id": deal_id,
        "buyer_id": deal['buyer_id'],
        "seller_id": deal['seller_id'],
        "reporter_id": message.from_user.id,
        "description": message.text,
        "status": "open"
    }

    deal['status'] = 'disputed'

    reporter_role = "Покупатель" if message.from_user.id == deal['buyer_id'] else "Продавец"
    complaint_text = (
        f"🚨 Новая жалоба #{complaint_id} на сделку #{deal_id}\n\n"
        f"👤 {reporter_role}: @{users[message.from_user.id]['username']} (ID: {message.from_user.id})\n"
        f"👤 Покупатель: @{deal['buyer_username']} (ID: {deal['buyer_id']})\n"
        f"👨‍💼 Продавец: @{deal['seller_username']} (ID: {deal['seller_id']})\n"
        f"💵 Сумма: {deal['price']} {deal['currency']}\n"
        f"📦 Товар: {deal['gift_link']}\n"
        f"📝 Описание проблемы:\n{message.text}"
    )

    support_keyboard = InlineKeyboardBuilder()
    support_keyboard.row(
        InlineKeyboardButton(text="✉️ Написать покупателю", url=f"tg://user?id={deal['buyer_id']}"),
        InlineKeyboardButton(text="✉️ Написать продавцу", url=f"tg://user?id={deal['seller_id']}")
    )
    support_keyboard.row(
        InlineKeyboardButton(text="✅ Вернуть средства покупателю", callback_data=f"resolve_dispute_refund_{complaint_id}"),
        InlineKeyboardButton(text="✅ Передать средства продавцу", callback_data=f"resolve_dispute_transfer_{complaint_id}")
    )

    for support_id in support_ids:
        await bot.send_message(
            chat_id=support_id,
            text=complaint_text,
            reply_markup=support_keyboard.as_markup()
        )

    await message.answer(
        "✅ Ваша жалоба отправлена в поддержку!\n"
        "Мы рассмотрим ваше обращение в течение 24 часов.\n"
        "Сделка приостановлена до решения модератора."
    )
    await state.clear()

# Разрешение спора модератором (возврат покупателю)
@dp.callback_query(F.data.startswith("resolve_dispute_refund_"))
async def resolve_dispute_refund(callback: types.CallbackQuery):
    complaint_id = int(callback.data.split("_")[3])
    complaint = complaints.get(complaint_id)
    if not complaint or complaint['status'] != 'open':
        await callback.message.answer("Жалоба уже обработана.")
        return
    deal_id = complaint['deal_id']
    deal = deals.get(deal_id)
    if not deal:
        await callback.message.answer("Сделка не найдена.")
        return

    buyer_id = deal['buyer_id']
    seller_id = deal['seller_id']
    price = deal['price']
    currency = deal['currency']

    # Возврат средств покупателю
    if currency == 'stars':
        if deal['status'] in ('payment_received', 'gift_sent'):
            users[buyer_id]['frozen_stars'] -= price
            users[buyer_id]['stars_balance'] += price
        elif deal['status'] == 'completed':
            seller_amount = price * (1 - COMMISSION_STARS)
            users[seller_id]['stars_balance'] -= seller_amount
            users[seller_id]['frozen_stars'] -= seller_amount
            users[buyer_id]['stars_balance'] += price
    else:  # TON
        if deal['status'] in ('payment_received', 'gift_sent'):
            users[buyer_id]['frozen_ton'] -= price
            users[buyer_id]['ton_balance'] += price
        elif deal['status'] == 'completed':
            seller_amount = price * (1 - COMMISSION_TON)
            users[seller_id]['ton_balance'] -= seller_amount
            users[seller_id]['frozen_ton'] -= seller_amount
            users[buyer_id]['ton_balance'] += price

    deal['status'] = 'disputed_refunded'
    complaint['status'] = 'resolved'

    await callback.message.edit_text(f"✅ Жалоба #{complaint_id} обработана. Средства возвращены покупателю.")
    await bot.send_message(buyer_id, f"По решению поддержки средства по сделке #{deal_id} возвращены вам.")
    await bot.send_message(seller_id, f"По решению поддержки сделка #{deal_id} отменена, средства возвращены покупателю.")

# Разрешение спора модератором (передача средств продавцу)
@dp.callback_query(F.data.startswith("resolve_dispute_transfer_"))
async def resolve_dispute_transfer(callback: types.CallbackQuery):
    complaint_id = int(callback.data.split("_")[3])
    complaint = complaints.get(complaint_id)
    if not complaint or complaint['status'] != 'open':
        await callback.message.answer("Жалоба уже обработана.")
        return
    deal_id = complaint['deal_id']
    deal = deals.get(deal_id)
    if not deal:
        await callback.message.answer("Сделка не найдена.")
        return

    buyer_id = deal['buyer_id']
    seller_id = deal['seller_id']
    price = deal['price']
    currency = deal['currency']

    if currency == 'stars':
        commission = COMMISSION_STARS
        seller_amount = price * (1 - commission)
        if deal['status'] in ('payment_received', 'gift_sent'):
            users[buyer_id]['frozen_stars'] -= price
            users[seller_id]['stars_balance'] += seller_amount
            users[seller_id]['frozen_stars'] = users[seller_id].get('frozen_stars', 0) + seller_amount
    else:  # TON
        commission = COMMISSION_TON
        seller_amount = price * (1 - commission)
        if deal['status'] in ('payment_received', 'gift_sent'):
            users[buyer_id]['frozen_ton'] -= price
            users[seller_id]['ton_balance'] += seller_amount
            users[seller_id]['frozen_ton'] = users[seller_id].get('frozen_ton', 0) + seller_amount

    deal['status'] = 'disputed_transferred'
    complaint['status'] = 'resolved'

    await callback.message.edit_text(f"✅ Жалоба #{complaint_id} обработана. Средства переданы продавцу (с вычетом комиссии).")
    await bot.send_message(buyer_id, f"По решению поддержки средства по сделке #{deal_id} переданы продавцу.")
    await bot.send_message(seller_id, f"По решению поддержки средства по сделке #{deal_id} зачислены вам (с вычетом комиссии).")

# Мои сделки
@dp.callback_query(F.data == "my_deals")
async def show_my_deals(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_deals = [d for d in deals.values() if d['buyer_id'] == user_id or d.get('seller_id') == user_id]

    if not user_deals:
        text = "📋 У вас пока нет сделок."
    else:
        text = "📋 Ваши сделки:\n\n"
        status_text = {
            "pending_moderator": "⏳ Ожидает модератора",
            "pending_acceptance": "⏳ Ожидает подтверждения продавцом",
            "accepted_by_seller": "✅ Продавец принял, ожидает оплаты",
            "payment_received": "💰 Оплачено, ожидается отправка",
            "gift_sent": "📦 Подарок отправлен, ожидает подтверждения",
            "completed": "✅ Завершена",
            "rejected": "❌ Отклонена",
            "disputed": "⚠️ Спор",
            "disputed_refunded": "⚠️ Спор (возврат)",
            "disputed_transferred": "⚠️ Спор (передано)"
        }
        for deal in user_deals:
            role = "Покупатель" if deal['buyer_id'] == user_id else "Продавец"
            currency = "звёзд" if deal['currency'] == 'stars' else "TON"
            text += f"🔹 Сделка #{deal['id']} ({role})\n"
            text += f"📎 {deal['gift_link']}\n"
            text += f"💎 {deal['price']} {currency}\n"
            text += f"📌 {status_text.get(deal['status'], deal['status'])}\n\n"

    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="Назад", callback_data="back_to_start"))
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())

# Баланс
@dp.callback_query(F.data == "check_balance")
async def check_balance_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_data = users.get(user_id, {})
    stars_balance = user_data.get("stars_balance", 0)
    stars_frozen = user_data.get("frozen_stars", 0)
    ton_balance = user_data.get("ton_balance", 0)
    ton_frozen = user_data.get("frozen_ton", 0)
    await callback.message.answer(
        f"💰 Ваш баланс:\n\n"
        f"⭐ Звёзды: {stars_balance}\n"
        f"❄️ Заморожено звёзд: {stars_frozen}\n"
        f"💎 Доступно звёзд: {stars_balance - stars_frozen}\n\n"
        f"💎 TON: {ton_balance:.2f}\n"
        f"❄️ Заморожено TON: {ton_frozen:.2f}\n"
        f"💎 Доступно TON: {ton_balance - ton_frozen:.2f}"
    )

# Поддержка
@dp.callback_query(F.data == "support")
async def show_support(callback: types.CallbackQuery):
    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="Назад", callback_data="back_to_start"))
    await callback.message.edit_text(
        "🆘 Служба поддержки: @support_bot\nОбращайтесь по любым вопросам",
        reply_markup=keyboard.as_markup()
    )

# Группы бота
@dp.callback_query(F.data == "groups")
async def show_groups(callback: types.CallbackQuery):
    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="Основная группа", url="https://t.me/telegrampay_group"))
    keyboard.add(InlineKeyboardButton(text="Новости", url="https://t.me/telegrampay_news"))
    keyboard.add(InlineKeyboardButton(text="Чат поддержки", url="https://t.me/telegrampay_support"))
    keyboard.add(InlineKeyboardButton(text="Назад", callback_data="back_to_start"))
    keyboard.adjust(1)
    await callback.message.edit_text(
        "👥 Группы бота:\n\nПрисоединяйтесь к нашим сообществам:",
        reply_markup=keyboard.as_markup(),
        disable_web_page_preview=True
    )

# Настройки
@dp.callback_query(F.data == "settings")
async def settings_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_data = users.get(user_id, {})
    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="Пополнить звёзды", callback_data="donate"))
    keyboard.add(InlineKeyboardButton(text="Пополнить TON", callback_data="donate_ton"))
    keyboard.add(InlineKeyboardButton(text="Проверить баланс", callback_data="check_balance"))
    keyboard.add(InlineKeyboardButton(text="Поддержка", url="https://t.me/support_bot"))
    keyboard.add(InlineKeyboardButton(text="Назад", callback_data="back_to_start"))
    keyboard.adjust(1)
    stars_balance = user_data.get("stars_balance", 0)
    stars_frozen = user_data.get("frozen_stars", 0)
    ton_balance = user_data.get("ton_balance", 0)
    ton_frozen = user_data.get("frozen_ton", 0)
    await callback.message.edit_text(
        f"⚙️ Настройки:\n\n"
        f"💰 Баланс:\n"
        f"⭐ Звёзды: {stars_balance} (доступно {stars_balance - stars_frozen})\n"
        f"💎 TON: {ton_balance:.2f} (доступно {ton_balance - ton_frozen:.2f})\n\n"
        f"🆘 Поддержка: @support_bot\n\n"
        f"Используйте команды:\n"
        f"/donate <сумма> - пополнить звёзды\n"
        f"/donate_ton <сумма> - пополнить TON\n"
        f"/get <сумма> - вывести звёзды",
        reply_markup=keyboard.as_markup(),
        disable_web_page_preview=True
    )

# Кнопки пополнения в настройках
@dp.callback_query(F.data == "donate")
async def donate_callback(callback: types.CallbackQuery):
    await callback.message.answer("Для пополнения звёзд используйте команду /donate <сумма>")

@dp.callback_query(F.data == "donate_ton")
async def donate_ton_callback(callback: types.CallbackQuery):
    await callback.message.answer("Для пополнения TON используйте команду /donate_ton <сумма>")

# Назад в стартовое меню
@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in users:
        users[user_id] = {
            "stars_balance": 0,
            "frozen_stars": 0,
            "ton_balance": 0,
            "frozen_ton": 0,
            "rating": 4.8,
            "reviews_count": 0,
            "deals_completed": 0,
            "username": callback.from_user.username or f"user_{user_id}",
        }
    keyboard = InlineKeyboardBuilder()
    buttons = [
        InlineKeyboardButton(text="➕ Создать сделку", callback_data="create_deal"),
        InlineKeyboardButton(text="📋 Мои сделки", callback_data="my_deals"),
        InlineKeyboardButton(text="💰 Баланс", callback_data="check_balance"),
        InlineKeyboardButton(text="❓ Поддержка", callback_data="support"),
        InlineKeyboardButton(text="👥 Группы бота", callback_data="groups"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")
    ]
    keyboard.add(*buttons)
    keyboard.adjust(2, 2, 2)
    await callback.message.edit_text(
        "Добро пожаловать! Здесь вы можете безопасно совершать сделки с NFT и подарками.",
        reply_markup=keyboard.as_markup()
    )

# Обработка отзывов (опционально)
@dp.callback_query(F.data.startswith("feedback_"))
async def start_feedback(callback: types.CallbackQuery, state: FSMContext):
    deal_id = int(callback.data.split("_")[1])
    deal = deals.get(deal_id)
    if deal and deal['status'] == "completed":
        await state.update_data(deal_id=deal_id, seller_id=deal['seller_id'])
        await callback.message.answer("Пожалуйста, напишите ваш отзыв о продавце:")
        await state.set_state(Form.feedback_text)

@dp.message(Form.feedback_text)
async def save_feedback(message: types.Message, state: FSMContext):
    data = await state.get_data()
    deal_id = data['deal_id']
    seller_id = data['seller_id']
    if "reviews" not in users[seller_id]:
        users[seller_id]["reviews"] = []
    users[seller_id]["reviews"].append({
        "text": message.text,
        "buyer_id": message.from_user.id,
        "deal_id": deal_id
    })
    users[seller_id]["rating"] = (users[seller_id].get("rating", 4.8) + 4.8) / 2
    users[seller_id]["reviews_count"] = users[seller_id].get("reviews_count", 0) + 1
    await message.answer("✅ Спасибо за ваш отзыв!")
    await state.clear()

# Автоматическое освобождение средств через 7 дней
async def auto_release_funds():
    while True:
        await asyncio.sleep(3600)
        current_time = datetime.now()
        for deal_id, deal in list(deals.items()):
            if (deal['status'] == "completed" and
                'seller_freeze_until' in deal and
                current_time > deal['seller_freeze_until']):
                seller_id = deal['seller_id']
                currency = deal['currency']
                if currency == 'stars':
                    if seller_id in users:
                        users[seller_id]["frozen_stars"] -= deal['price'] * (1 - COMMISSION_STARS)
                else:  # TON
                    if seller_id in users:
                        users[seller_id]["frozen_ton"] -= deal['price'] * (1 - COMMISSION_TON)
                del deal['seller_freeze_until']
                try:
                    await bot.send_message(
                        seller_id,
                        f"✅ Средства от сделки #{deal_id} теперь доступны для вывода."
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить продавца {seller_id}: {e}")

# Обработка запросов на вывод звёзд (еженедельная разморозка)
async def process_pending_withdrawals():
    while True:
        await asyncio.sleep(3600)
        current_time = datetime.now()
        for user_id, withdrawals in list(pending_withdrawals.items()):
            for withdrawal in list(withdrawals):
                if (withdrawal["status"] == "pending" and
                    current_time - withdrawal["timestamp"] > timedelta(days=7)):
                    withdrawal["status"] = "completed"
                    logger.info(f"Вывод {withdrawal['amount']} звёзд для пользователя {user_id} выполнен")
                    try:
                        await bot.send_message(
                            user_id,
                            f"✅ Вывод {withdrawal['amount']} звёзд выполнен.\nID запроса: {withdrawal['id']}"
                        )
                    except Exception as e:
                        logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")

# Запуск бота
async def main():
    asyncio.create_task(auto_release_funds())
    asyncio.create_task(process_pending_withdrawals())
    asyncio.create_task(check_ton_invoices())
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.info("Бот запускается...")

    asyncio.run(main())

