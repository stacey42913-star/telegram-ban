import asyncio
import logging
import razorpay
import firebase_admin
from firebase_admin import credentials, db as rtdb
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ==================== 🛠️ CONFIGURATION ====================
BOT_TOKEN = "8720972029:AAHMfULogeyCm3fXW1gw4_DoNwk2n8qZg4g"          # BotFather token yahan daalein
RAZORPAY_KEY_ID = "rzp_live_TOKzVyJ4C0Fzy7"      # Razorpay Key ID
RAZORPAY_KEY_SECRET = "o4U9ePMU188qdBWFY2mAXDcp" # Razorpay Key Secret

API_ID = 37718717
API_HASH = "481fd5a3111efe80e3a6c5b18ce0e8e8"
FIREBASE_URL = "https://file-29e6f-default-rtdb.firebaseio.com/"
ADMIN_USER_ID = 8972274122                   # Apni Telegram User ID yahan daalein
# ==========================================================

# --- FIREBASE RTDB INITIALIZATION ---
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_URL
})

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
razor_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
logging.basicConfig(level=logging.INFO)

USER_STATES = {}

# --- HELPER: AVAILABLE NUMBERS COUNT NIKALNA ---
def get_available_count():
    ref = rtdb.reference("numbers")
    numbers_data = ref.get()
    if not numbers_data:
        return 0
    count = sum(1 for data in numbers_data.values() if data.get("status") == "available")
    return count

# --- HELPER: RTDB SE AVAILABLE NUMBER LENA ---
def get_available_number():
    ref = rtdb.reference("numbers")
    numbers_data = ref.get()
    if not numbers_data:
        return None
    for key, data in numbers_data.items():
        if data.get("status") == "available":
            ref.child(key).update({"status": "sold"})
            return {
                "key": key,
                "number": data["number"],
                "session_string": data["session_string"]
            }
    return None

# --- /START COMMAND ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    stock_count = get_available_count()
    
    builder = InlineKeyboardBuilder()
    if stock_count > 0:
        builder.button(text=f"🛒 Buy Telegram Number (₹1 Testing)", callback_data="buy_number")
    else:
        builder.button(text="❌ Stock Out", callback_data="no_stock")
        
    await message.answer(
        f"👋 **Welcome!**\n\n"
        f"Humare bot se aap automatic Telegram number kharid sakte hain.\n"
        f"📦 **Live Stock Available:** `{stock_count} Numbers`\n"
        f"💰 **Price:** ₹1 (Testing Mode)\n\n"
        f"⚡ Payment ke turant baad number aur automatic OTP milega.",
        reply_markup=builder.as_markup()
    )

# --- ADMIN COMMAND: NUMBER ADD KARNE KE LIYE ---
@dp.message(Command("add"))
async def add_number_cmd(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        await message.answer("❌ Aap is command ko use nahi kar sakte!")
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("⚠️ Sahi format use karein:\n`/add <number> <session_string>`")
        return

    phone = args[1]
    session_str = args[2]

    ref = rtdb.reference("numbers")
    ref.push({
        "number": phone,
        "status": "available",
        "session_string": session_str
    })

    await message.answer(f"✅ Success! Number `{phone}` stock mein add kar diya gaya hai.")

# --- STOCK OUT CLICK HANDLE ---
@dp.callback_query(F.data == "no_stock")
async def no_stock_alert(callback: types.CallbackQuery):
    await callback.answer("❌ Filhal stock khatam ho gaya hai. Kripya baad mein try karein.", show_alert=True)

# --- BUY NUMBER & PAYMENT LINK (₹1 SET) ---
@dp.callback_query(F.data == "buy_number")
async def process_buy(callback: types.CallbackQuery):
    item = get_available_number()
    if not item:
        await callback.message.answer("❌ Stock khatam ho gaya hai!")
        await callback.answer()
        return

    user_id = callback.from_user.id
    phone_number = item["number"]
    session_string = item["session_string"]
    db_key = item["key"]

    try:
        payment_data = {
            "amount": 100,  # 100 paise = ₹1 (Testing ke liye)
            "currency": "INR",
            "description": f"Telegram Number: {phone_number}",
            "customer": {"name": f"User {user_id}", "contact": "9999999999"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False
        }
        
        link_response = razor_client.payment_link.create(payment_data)
        payment_url = link_response["short_url"]
        plink_id = link_response["id"]

        USER_STATES[user_id] = {
            "db_key": db_key,
            "number": phone_number,
            "session_string": session_string,
            "plink_id": plink_id
        }

        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Pay ₹1 Now", url=payment_url)
        builder.button(text="🔄 Main Payment Kar Diya (Verify)", callback_data=f"verify_{user_id}")

        await callback.message.answer(
            f"📱 **Aapka Assigned Number:** `{phone_number}`\n\n"
            f"1️⃣ Upar diye gaye **'Pay ₹1 Now'** button par click karke payment poori karein.\n"
            f"2️⃣ Payment ke baad **'Main Payment Kar Diya'** par click karein.",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        rtdb.reference(f"numbers/{db_key}").update({"status": "available"})
        await callback.message.answer(f"❌ Error: {str(e)}")
    
    await callback.answer()

# --- VERIFY PAYMENT & GIVE NUMBER ---
@dp.callback_query(F.data.startswith("verify_"))
async def verify_payment(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])

    if user_id not in USER_STATES:
        await callback.message.answer("❌ Session expired. Please `/start` se dubara try karein.")
        await callback.answer()
        return

    user_data = USER_STATES[user_id]
    plink_id = user_data["plink_id"]

    try:
        link_info = razor_client.payment_link.fetch(plink_id)
        status = link_info.get("status")

        if status == "paid":
            phone = user_data["number"]
            session_string = user_data["session_string"]

            await callback.message.answer(
                f"✅ **Payment Successful!** 🎉\n\n"
                f"📱 **Aapka Number:** `{phone}`\n\n"
                f"Ab aap is number se Telegram me login karein. Jaise hi OTP aayega, bot yahan bhej dega! ⏳"
            )

            asyncio.create_task(listen_otp_for_user(user_id, session_string))
            del USER_STATES[user_id]
        else:
            await callback.message.answer(
                "⏳ **Payment abhi tak confirm nahi hui hai!**\n"
                "Kripya payment poori karke thodi der baad dubara 'Verify' dabayein."
            )
    except Exception as e:
        await callback.message.answer(f"❌ Verification Error: {str(e)}")

    await callback.answer()

# --- AUTOMATIC OTP LISTENER ---
async def listen_otp_for_user(user_id, session_string):
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()

        @client.on(events.NewMessage(chats=777000))
        async def otp_handler(event):
            text = event.message.message
            if "login code" in text.lower() or "code" in text.lower():
                await bot.send_message(
                    user_id,
                    f"🔑 **Aapka Telegram OTP Mil Gaya hai!**\n\n`{text}`"
                )
                await client.disconnect()

        await asyncio.sleep(300)
        await client.disconnect()
    except Exception as e:
        logging.error(f"OTP Error: {e}")

# --- MAIN RUNNER ---
async def main():
    print("🤖 Testing Bot Start Ho Gaya Hai...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
