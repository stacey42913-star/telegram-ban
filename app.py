


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
