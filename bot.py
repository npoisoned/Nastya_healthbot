import os
import logging
import datetime as dt
from typing import Optional

import requests
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.filters import Command, CommandObject
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


BOT_TOKEN = os.getenv("BOT_TOKEN")
OWM_API_KEY = os.getenv("OWM_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Нет BOT_TOKEN в переменных окружения")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

users: dict[int, dict] = {}


class LogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            uid = event.from_user.id if event.from_user else None
            log.info(f"user={uid} text={event.text}")
        return await handler(event, data)


def today_key() -> str:
    return dt.date.today().isoformat()


def ensure_user(user_id: int) -> None:
    if user_id not in users:
        users[user_id] = {"profile": None, "days": {}}


def ensure_day(user_id: int, day: str) -> None:
    ensure_user(user_id)
    days = users[user_id]["days"]
    if day not in days:
        days[day] = {
            "water_ml": 0,
            "cal_in": 0.0,
            "cal_out": 0.0,
            "workout_extra_water_ml": 0,
            "foods": [],
            "workouts": [],
        }


def profile_of(user_id: int) -> Optional[dict]:
    ensure_user(user_id)
    return users[user_id]["profile"]


def get_temp_c(city: str) -> Optional[float]:
    if not OWM_API_KEY:
        return None
    try:
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {"q": city, "appid": OWM_API_KEY, "units": "metric", "lang": "ru"}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        return float(data["main"]["temp"])
    except Exception:
        return None


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum() or ch.isspace()).strip()


def get_food_kcal_100g(query: str) -> Optional[dict]:
    try:
        url = "https://world.openfoodfacts.org/cgi/search.pl"
        params = {"action": "process", "search_terms": query, "json": "true", "page_size": 10}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        products = data.get("products", []) or []

        qn = _norm(query)
        best = None  # (score, name, kcal)

        for p in products:
            nutr = p.get("nutriments") or {}
            kcal = nutr.get("energy-kcal_100g")

            if kcal is None:
                kj = nutr.get("energy_100g")
                if kj is None:
                    continue
                kcal = float(kj) / 4.184

            name = p.get("product_name") or p.get("generic_name") or "Продукт"
            nn = _norm(name)

            score = 0
            if qn and qn in nn:
                score += 3
            for w in qn.split():
                if w and w in nn:
                    score += 2

            cand = (score, name, float(kcal))
            if best is None or cand[0] > best[0]:
                best = cand

        if best is None:
            return None
        return {"name": best[1], "kcal_100g": best[2]}
    except Exception:
        return None


def calc_water_goal_ml(profile: dict, temp_c: Optional[float], workout_extra_ml: int) -> int:
    base = int(profile["weight"] * 30)
    activity_extra = int((profile["activity_min"] / 30) * 500)

    heat_extra = 0
    if temp_c is not None:
        if temp_c > 30:
            heat_extra = 1000
        elif temp_c > 25:
            heat_extra = 500

    return base + activity_extra + heat_extra + int(workout_extra_ml)


def calc_calorie_goal(profile: dict) -> int:
    w, h, a = profile["weight"], profile["height"], profile["age"]
    base = 10 * w + 6.25 * h - 5 * a
    activity_extra = (profile["activity_min"] / 30) * 200
    goal = int(base + activity_extra)

    override = profile.get("calorie_goal_override")
    if override is not None:
        goal = int(override)
    return goal


MET = {
    "бег": 9.8,
    "ходьба": 3.5,
    "велосипед": 7.5,
    "плавание": 8.0,
    "силовая": 6.0,
    "йога": 3.0,
}


def calc_burned_kcal(workout_type: str, minutes: int, weight: float) -> int:
    met = MET.get(workout_type.lower(), 6.0)
    return int(round(met * weight * (minutes / 60)))


class ProfileFSM(StatesGroup):
    weight = State()
    height = State()
    age = State()
    activity = State()
    city = State()
    cal_goal = State()


class FoodFSM(StatesGroup):
    manual_kcal_100g = State()
    grams = State()


bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(LogMiddleware())


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Команды:\n"
        "/set_profile\n"
        "/log_water 250\n"
        "/log_food банан\n"
        "/log_workout бег 30\n"
        "/check_progress"
    )


@dp.message(Command("set_profile"))
async def set_profile(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ProfileFSM.weight)
    await message.answer("Введите вес (кг):")


@dp.message(ProfileFSM.weight)
async def prof_weight(message: Message, state: FSMContext):
    try:
        w = float(message.text.replace(",", "."))
        if w <= 0 or w > 400:
            raise ValueError
    except Exception:
        return await message.answer("Введите корректный вес (например 80).")

    await state.update_data(weight=w)
    await state.set_state(ProfileFSM.height)
    await message.answer("Введите рост (см):")


@dp.message(ProfileFSM.height)
async def prof_height(message: Message, state: FSMContext):
    try:
        h = float(message.text.replace(",", "."))
        if h <= 0 or h > 260:
            raise ValueError
    except Exception:
        return await message.answer("Введите корректный рост (например 184).")

    await state.update_data(height=h)
    await state.set_state(ProfileFSM.age)
    await message.answer("Введите возраст:")


@dp.message(ProfileFSM.age)
async def prof_age(message: Message, state: FSMContext):
    try:
        a = int(message.text)
        if a <= 0 or a > 120:
            raise ValueError
    except Exception:
        return await message.answer("Введите корректный возраст (например 26).")

    await state.update_data(age=a)
    await state.set_state(ProfileFSM.activity)
    await message.answer("Минут активности в день:")


@dp.message(ProfileFSM.activity)
async def prof_activity(message: Message, state: FSMContext):
    try:
        m = int(message.text)
        if m < 0 or m > 600:
            raise ValueError
    except Exception:
        return await message.answer("Введите корректное число минут (например 45).")

    await state.update_data(activity_min=m)
    await state.set_state(ProfileFSM.city)
    await message.answer("Город (для погоды):")


@dp.message(ProfileFSM.city)
async def prof_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not city:
        return await message.answer("Введите город текстом.")
    await state.update_data(city=city)
    await state.set_state(ProfileFSM.cal_goal)
    await message.answer("Цель калорий вручную? Число или 'нет':")


@dp.message(ProfileFSM.cal_goal)
async def prof_cal_goal(message: Message, state: FSMContext):
    txt = (message.text or "").strip().lower()
    override = None
    if txt not in ("нет", "no", "n"):
        try:
            override = int(txt)
            if override < 800 or override > 6000:
                raise ValueError
        except Exception:
            return await message.answer("Либо число (например 2500), либо 'нет'.")

    data = await state.get_data()
    profile = {
        "weight": data["weight"],
        "height": data["height"],
        "age": data["age"],
        "activity_min": data["activity_min"],
        "city": data["city"],
        "calorie_goal_override": override,
    }

    ensure_user(message.from_user.id)
    users[message.from_user.id]["profile"] = profile

    day = today_key()
    ensure_day(message.from_user.id, day)
    d = users[message.from_user.id]["days"][day]

    temp = get_temp_c(profile["city"])
    water_goal = calc_water_goal_ml(profile, temp, d["workout_extra_water_ml"])
    cal_goal = calc_calorie_goal(profile)

    temp_str = "нет данных" if temp is None else f"{temp:.1f} C"
    await state.clear()
    await message.answer(
        "Профиль сохранён.\n"
        f"Температура: {temp_str}\n"
        f"Норма воды: {water_goal} мл\n"
        f"Цель калорий: {cal_goal} ккал"
    )


@dp.message(Command("log_water"))
async def log_water(message: Message, command: CommandObject):
    profile = profile_of(message.from_user.id)
    if not profile:
        return await message.answer("Сначала /set_profile")

    if not command.args:
        return await message.answer("Формат: /log_water 250")

    try:
        ml = int(command.args.strip())
        if ml <= 0 or ml > 5000:
            raise ValueError
    except Exception:
        return await message.answer("Введите корректное число мл (например 250).")

    day = today_key()
    ensure_day(message.from_user.id, day)
    d = users[message.from_user.id]["days"][day]
    d["water_ml"] += ml

    temp = get_temp_c(profile["city"])
    water_goal = calc_water_goal_ml(profile, temp, d["workout_extra_water_ml"])
    left = max(0, water_goal - d["water_ml"])

    await message.answer(
        "Вода записана.\n"
        f"Выпито: {d['water_ml']} / {water_goal} мл\n"
        f"Осталось: {left} мл"
    )


@dp.message(Command("log_food"))
async def log_food(message: Message, command: CommandObject, state: FSMContext):
    profile = profile_of(message.from_user.id)
    if not profile:
        return await message.answer("Сначала /set_profile")

    if not command.args:
        return await message.answer("Формат: /log_food банан")

    query = command.args.strip()
    info = get_food_kcal_100g(query)

    await state.clear()
    if info is None:
        await state.update_data(food_name=query)
        await state.set_state(FoodFSM.manual_kcal_100g)
        return await message.answer("Не нашёл продукт. Введите ккал на 100 г вручную:")

    await state.update_data(food_name=info["name"], kcal_100g=info["kcal_100g"])
    await state.set_state(FoodFSM.grams)
    await message.answer(f"{info['name']} {info['kcal_100g']:.0f} ккал/100г. Сколько грамм?")


@dp.message(FoodFSM.manual_kcal_100g)
async def food_manual_kcal(message: Message, state: FSMContext):
    try:
        kcal_100g = float(message.text.replace(",", "."))
        if kcal_100g < 0 or kcal_100g > 2000:
            raise ValueError
    except Exception:
        return await message.answer("Введите корректное число (например 89).")

    await state.update_data(kcal_100g=kcal_100g)
    await state.set_state(FoodFSM.grams)

    data = await state.get_data()
    await message.answer(f"{data['food_name']} {kcal_100g:.0f} ккал/100г. Сколько грамм?")


@dp.message(FoodFSM.grams)
async def food_grams(message: Message, state: FSMContext):
    profile = profile_of(message.from_user.id)
    if not profile:
        await state.clear()
        return await message.answer("Сначала /set_profile")

    try:
        grams = float(message.text.replace(",", "."))
        if grams <= 0 or grams > 5000:
            raise ValueError
    except Exception:
        return await message.answer("Введите граммы числом (например 150).")

    data = await state.get_data()
    name = data["food_name"]
    kcal_100g = float(data["kcal_100g"])
    kcal = kcal_100g * grams / 100.0

    day = today_key()
    ensure_day(message.from_user.id, day)
    d = users[message.from_user.id]["days"][day]
    d["cal_in"] += kcal
    d["foods"].append((name, grams, kcal))

    await state.clear()
    await message.answer(f"Еда записана: {name}, {grams:.0f} г, {kcal:.0f} ккал.")


@dp.message(Command("log_workout"))
async def log_workout(message: Message, command: CommandObject):
    profile = profile_of(message.from_user.id)
    if not profile:
        return await message.answer("Сначала /set_profile")

    if not command.args:
        return await message.answer("Формат: /log_workout бег 30")

    parts = command.args.split()
    if len(parts) < 2:
        return await message.answer("Формат: /log_workout <тип> <минуты>")

    workout_type = " ".join(parts[:-1]).strip()
    try:
        minutes = int(parts[-1])
        if minutes <= 0 or minutes > 600:
            raise ValueError
    except Exception:
        return await message.answer("Минуты должны быть числом (например 30).")

    burned = calc_burned_kcal(workout_type, minutes, profile["weight"])
    extra_water = int((minutes / 30) * 200)

    day = today_key()
    ensure_day(message.from_user.id, day)
    d = users[message.from_user.id]["days"][day]
    d["cal_out"] += burned
    d["workout_extra_water_ml"] += extra_water
    d["workouts"].append((workout_type, minutes, burned))

    await message.answer(
        "Тренировка записана.\n"
        f"Тип: {workout_type}\n"
        f"Время: {minutes} мин\n"
        f"Сожжено: {burned} ккал\n"
        f"Доп. вода к норме: {extra_water} мл"
    )


@dp.message(Command("check_progress"))
async def check_progress(message: Message):
    profile = profile_of(message.from_user.id)
    if not profile:
        return await message.answer("Сначала /set_profile")

    day = today_key()
    ensure_day(message.from_user.id, day)
    d = users[message.from_user.id]["days"][day]

    temp = get_temp_c(profile["city"])
    water_goal = calc_water_goal_ml(profile, temp, d["workout_extra_water_ml"])
    cal_goal = calc_calorie_goal(profile)

    water_left = max(0, water_goal - d["water_ml"])
    cal_balance = d["cal_in"] - d["cal_out"]
    cal_left = cal_goal - cal_balance

    temp_str = "нет данных" if temp is None else f"{temp:.1f} C"

    await message.answer(
        "Прогресс за сегодня:\n\n"
        f"Температура: {temp_str}\n\n"
        "Вода:\n"
        f"- Выпито: {d['water_ml']} мл из {water_goal} мл\n"
        f"- Осталось: {water_left} мл\n\n"
        "Калории:\n"
        f"- Потреблено: {d['cal_in']:.0f} ккал\n"
        f"- Сожжено: {d['cal_out']:.0f} ккал\n"
        f"- Баланс: {cal_balance:.0f} ккал\n"
        f"- Цель: {cal_goal} ккал\n"
        f"- Осталось до цели: {cal_left:.0f} ккал"
    )


async def main():
    log.info("bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
