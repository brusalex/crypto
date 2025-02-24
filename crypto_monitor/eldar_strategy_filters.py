import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Tuple, Optional, Any

import aiohttp
import ccxt
import pandas as pd


@dataclass
class SignalState:
    """Класс для хранения состояния сигнала"""
    time: datetime
    price: float
    type: str
    stop_loss: Optional[float]
    take_profit: Optional[float]
    trailing_activated: bool = False


# Получение URL сервиса Telegram с переменных окружения или использование значения по умолчанию
TELEGRAM_SERVICE_URL = os.getenv('TELEGRAM_SERVICE_URL', 'http://localhost:8000')


async def send_telegram_alert(message: str) -> Dict[str, Any]:
    """Отправка уведомления в Telegram"""
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{TELEGRAM_SERVICE_URL}/send_message",
                                json={"text": message}) as response:
            return await response.json()


class ElderTripleScreenStrategy:
    def __init__(self,
                 exchange_id: str = 'binance',
                 check_interval: int = 1200,
                 mode: str = 'INTRADAY',
                 symbol: str = 'BTC/USDT'):
        """
        Инициализация стратегии Elder Triple Screen

        Args:
            exchange_id: ID биржи для использования (по умолчанию 'binance')
            check_interval: Интервал проверки в секундах
            mode: Режим работы ('INTRADAY' или 'SWING')
            symbol: Торговая пара
        """
        self.symbol = symbol
        self.timeframes = {
            'SWING': {'trend': '1w', 'entry': '1d', 'intraday': '1h'},
            'INTRADAY': {'trend': '1d', 'entry': '1h', 'intraday': '15m'}
        }

        # Параметры риск-менеджмента
        self.risk_percent = 0.02
        self.trailing_start = 0.01
        self.trailing_step = 0.005
        self.min_profit_ratio = 1.5
        self.max_retries = 3  # Максимум попыток для API-запросов

        # Минимальное количество свечей
        self.min_candles = {'trend': 104, 'entry': 200, 'intraday': 50}

        # Состояния
        self.active_signals: Dict[str, SignalState] = {}
        self.current_positions: Dict[str, Dict] = {}

        self.setup_logging()
        self.exchange = self.init_exchange(exchange_id)
        self.current_mode = mode
        self.check_interval = check_interval

    def init_exchange(self, exchange_id: str) -> ccxt.Exchange:
        """Инициализация подключения к бирже с обработкой ошибок"""
        exchange = getattr(ccxt, exchange_id)({
            'enableRateLimit': True,
            'options': {'adjustForTimeDifference': True}
        })
        exchange.load_markets()
        return exchange

    def setup_logging(self):
        """Расширенная настройка логирования"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
            handlers=[
                logging.FileHandler('elder_strategy.log', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        if sys.platform.startswith('win'):
            sys.stdout.reconfigure(encoding='utf-8')
        self.logger = logging.getLogger(__name__)

    async def safe_fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Безопасный запрос исторических данных с повторными попытками"""
        for _ in range(self.max_retries):
            try:
                await asyncio.sleep(1)  # Защита от rate limit
                # Убираем await, так как fetch_ohlcv синхронный
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                self.logger.warning(f"Ошибка получения данных: {str(e)}. Повторная попытка...")
                await asyncio.sleep(5)
        raise ConnectionError("Не удалось получить данные")

    def validate_data(self, df: pd.DataFrame, required_length: int, timeframe: str) -> bool:
        """Улучшенная валидация данных"""
        if df.empty or len(df) < required_length:
            self.logger.error(f"Недостаточно данных для {timeframe}")
            return False

        # Проверка аномалий в OHLC
        ohlc_checks = (
                (df['high'] < df['low']).any() |
                (df['close'] > df['high']).any() |
                (df['close'] < df['low']).any()
        )
        return not ohlc_checks

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Оптимизированный расчет индикаторов"""
        try:
            # EMA и MACD
            df['ema12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['ema26'] = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = df['ema12'] - df['ema26']
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_hist'] = df['macd'] - df['macd_signal']

            # Стохастик
            period = 14
            df['lowest_low'] = df['low'].rolling(period).min()
            df['highest_high'] = df['high'].rolling(period).max()
            df['%K'] = ((df['close'] - df['lowest_low']) /
                        (df['highest_high'] - df['lowest_low'])) * 100
            df['%D'] = df['%K'].rolling(3).mean()

            # Индикаторы Элдера
            df['ema13'] = df['close'].ewm(span=13, adjust=False).mean()
            df['bull_power'] = df['high'] - df['ema13']
            df['bear_power'] = df['low'] - df['ema13']
            df['force_index'] = df['close'].diff() * df['volume']
            df['force_index_ema2'] = df['force_index'].ewm(span=2, adjust=False).mean()

            return df.dropna()
        except Exception as e:
            self.logger.error(f"Ошибка расчета индикаторов: {str(e)}")
            raise

    async def analyze_trend(self, symbol: str) -> dict:
        """Анализ тренда на основе динамики MACD"""
        try:
            timeframe = self.timeframes[self.current_mode]['trend']
            df = await self.safe_fetch_ohlcv(symbol, timeframe, self.min_candles['trend'])

            if not self.validate_data(df, self.min_candles['trend'], timeframe):
                return {'trend': 'neutral'}

            df = self.calculate_indicators(df)
            last_hist = df['macd_hist'].iloc[-1]
            prev_hist = df['macd_hist'].iloc[-2]

            if last_hist > prev_hist:
                trend = 'bullish'
            elif last_hist < prev_hist:
                trend = 'bearish'
            else:
                trend = 'neutral'

            return {'trend': trend}

        except Exception as e:
            self.logger.error(f"Ошибка анализа тренда: {str(e)}")
            return {'trend': 'neutral'}

    async def check_entry_signal(self, symbol: str, trend: dict) -> Optional[SignalState]:
        """Поиск точек входа с улучшенными фильтрами"""
        try:
            timeframe = self.timeframes[self.current_mode]['entry']
            df = await self.safe_fetch_ohlcv(symbol, timeframe, self.min_candles['entry'])

            if not self.validate_data(df, self.min_candles['entry'], timeframe):
                return None

            df = self.calculate_indicators(df)
            last = df.iloc[-1]
            prev = df.iloc[-2]

            if trend['trend'] == 'bullish':
                conditions = [
                    last['%K'] < 30 and last['%K'] > prev['%K'],
                    last['force_index_ema2'] < 0 and last['force_index_ema2'] > prev['force_index_ema2']
                ]
                signal_type = 'LONG'
            elif trend['trend'] == 'bearish':
                conditions = [
                    last['%K'] > 70 and last['%K'] < prev['%K'],
                    last['force_index_ema2'] > 0 and last['force_index_ema2'] < prev['force_index_ema2']
                ]
                signal_type = 'SHORT'
            else:
                return None

            if all(conditions):
                # Проверяем третий фильтр на внутридневном таймфрейме
                if await self.confirm_entry(symbol, signal_type):
                    stop_loss, take_profit = self.calculate_stop_levels(
                        last['close'], signal_type, df
                    )
                    return SignalState(
                        time=datetime.now(),
                        price=last['close'],
                        type=signal_type,
                        stop_loss=stop_loss,
                        take_profit=take_profit
                    )
            return None

        except Exception as e:
            self.logger.error(f"Ошибка поиска входа: {str(e)}", exc_info=True)
            return None

    def calculate_stop_levels(self, price: float, signal_type: str, df: pd.DataFrame) -> Tuple[float, float]:
        """Расчет стоп-лосса и тейк-профита с использованием ATR"""
        try:
            atr_period = 14

            # Рассчитываем True Range (TR) для каждой свечи
            df['prev_close'] = df['close'].shift()  # Предыдущее закрытие
            df['tr1'] = df['high'] - df['low']  # Разница между high и low
            df['tr2'] = abs(df['high'] - df['prev_close'])  # Разница между high и предыдущим close
            df['tr3'] = abs(df['low'] - df['prev_close'])  # Разница между low и предыдущим close
            df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)  # Максимальное значение из трех

            # Рассчитываем ATR как среднее значение TR за последние 14 свечей
            atr = df['tr'].rolling(window=atr_period).mean().iloc[-1]

            # Рассчитываем стоп-лосс и тейк-профит на основе ATR
            if signal_type == 'LONG':
                stop_loss = price - 2 * atr
                take_profit = price + 3 * atr
            else:
                stop_loss = price + 2 * atr
                take_profit = price - 3 * atr

            return round(stop_loss, 4), round(take_profit, 4)

        except Exception as e:
            self.logger.error(f"Ошибка расчета уровней стоп-лосса и тейк-профита: {str(e)}")
            raise

    async def manage_risk(self, symbol: str):
        """Управление открытыми позициями"""
        if symbol not in self.active_signals:
            return

        signal = self.active_signals[symbol]
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            current_price = ticker['last']

            # Обновление трейлинг-стопа
            self.update_trailing_stop(signal, current_price)

            # Проверка условий выхода
            if (signal.type == 'LONG' and
                    (current_price <= signal.stop_loss or current_price >= signal.take_profit)):
                await self.close_position(symbol, current_price)

            elif (signal.type == 'SHORT' and
                  (current_price >= signal.stop_loss or current_price <= signal.take_profit)):
                await self.close_position(symbol, current_price)

        except Exception as e:
            self.logger.error(f"Ошибка управления рисками: {str(e)}")

    async def confirm_entry(self, symbol: str, signal_type: str) -> bool:
        """Третий фильтр - подтверждение сигнала на более низком таймфрейме"""
        try:
            timeframe = self.timeframes[self.current_mode]['intraday']
            df = await self.safe_fetch_ohlcv(symbol, timeframe, self.min_candles['intraday'])

            if not self.validate_data(df, self.min_candles['intraday'], timeframe):
                return False

            df['ema_short'] = df['close'].ewm(span=5, adjust=False).mean()  # 5-периодная EMA
            last = df.iloc[-1]
            prev = df.iloc[-2]

            if signal_type == 'LONG':
                # Цена пересекает EMA вверх
                return prev['close'] < prev['ema_short'] and last['close'] > last['ema_short']
            elif signal_type == 'SHORT':
                # Цена пересекает EMA вниз
                return prev['close'] > prev['ema_short'] and last['close'] < last['ema_short']

            return False

        except Exception as e:
            self.logger.error(f"Ошибка подтверждения входа: {str(e)}")
            return False

    def update_trailing_stop(self, signal: SignalState, current_price: float):
        """Динамическое обновление трейлинг-стопа"""
        if signal.type == 'LONG':
            profit = (current_price - signal.price) / signal.price
            new_stop = current_price * (1 - self.trailing_step)

            if profit >= self.trailing_start and new_stop > signal.stop_loss:
                signal.stop_loss = new_stop
                signal.trailing_activated = True

        elif signal.type == 'SHORT':
            profit = (signal.price - current_price) / signal.price
            new_stop = current_price * (1 + self.trailing_step)

            if profit >= self.trailing_start and new_stop < signal.stop_loss:
                signal.stop_loss = new_stop
                signal.trailing_activated = True

    async def close_position(self, symbol: str, price: float):
        """Закрытие позиции с обработкой ордера"""
        try:
            if symbol in self.active_signals:
                signal = self.active_signals.pop(symbol)
                order = await self.exchange.create_order(
                    symbol,
                    'market',
                    'sell' if signal.type == 'LONG' else 'buy',
                    self.calculate_position_size(price, signal.stop_loss),
                    price
                )
                self.logger.info(f"Закрыта позиция {symbol}: {order}")
        except Exception as e:
            self.logger.error(f"Ошибка закрытия позиции: {str(e)}")

    def calculate_position_size(self, entry: float, stop: float) -> float:
        """Расчет размера позиции на основе риска"""
        risk_amount = self.risk_percent * self.get_portfolio_value()
        risk_per_unit = abs(entry - stop)
        return round(risk_amount / risk_per_unit, 4) if risk_per_unit != 0 else 0

    def get_portfolio_value(self) -> float:
        """Получение текущей стоимости портфеля (заглушка)"""
        return 10000.0  # Реализовать логику получения баланса

    async def run_strategy(self):
        """Основной цикл торговой стратегии"""
        startup_message = f"""
        🚀 Elder Triple Screen Strategy Started!

        💱 Trading Pair: {self.symbol}
        📊 Mode: {self.current_mode}

        ⏱️ Timeframes:
           • Trend: {self.timeframes[self.current_mode]['trend']}
           • Entry: {self.timeframes[self.current_mode]['entry']}
           • Intraday: {self.timeframes[self.current_mode]['intraday']}

        🔄 Check interval: {self.check_interval} seconds
        """

        self.logger.info(startup_message)
        await send_telegram_alert(startup_message)

        while True:
            try:
                # Анализ тренда
                trend = await self.analyze_trend(self.symbol)
                self.logger.info(f"[{self.current_mode}] Текущий тренд: {trend['trend']}")

                # Поиск сигнала
                signal = await self.check_entry_signal(self.symbol, trend)

                if signal:
                    self.active_signals[self.symbol] = signal
                    signal_message = f"""
                    🚨 [{self.current_mode}] Cигнал на {self.symbol}!
                    📈 Тип: {signal.type}
                    💰 Цена: {signal.price:.2f}
                    🛑 Stop Loss: {signal.stop_loss:.2f}
                    🎯 Take Profit: {signal.take_profit:.2f}
                    ⏰ Время: {signal.time}
                    """
                    self.logger.info(f"[{self.current_mode}] Встряска на {self.symbol}! {signal_message}")
                    await send_telegram_alert(signal_message)

                # Управление рисками (опционально)
                # await self.manage_risk(self.symbol)

                # Пауза между итерациями
                await asyncio.sleep(self.check_interval)

            except ccxt.NetworkError as e:
                self.logger.error(f"[{self.current_mode}] Ошибка сети: {str(e)}")
                await asyncio.sleep(60)
            except ccxt.ExchangeError as e:
                self.logger.error(f"[{self.current_mode}] Ошибка биржи: {str(e)}")
                await asyncio.sleep(300)
            except Exception as e:
                self.logger.error(f"[{self.current_mode}] Критическая ошибка: {str(e)}", exc_info=True)
                await asyncio.sleep(600)


class ElderStrategyManager:
    """Менеджер для запуска нескольких экземпляров стратегии Elder Triple Screen"""

    def __init__(self,
                 exchange_id: str = 'binance',
                 symbol: str = 'BTC/USDT',
                 intraday_interval: int = 1200,
                 swing_interval: int = 3600,
                 run_intraday: bool = True,
                 run_swing: bool = True):
        """
        Инициализация менеджера стратегий

        Args:
            exchange_id: ID биржи для использования
            symbol: Торговая пара
            intraday_interval: Интервал проверки для внутридневной торговли (в секундах)
            swing_interval: Интервал проверки для свинг-торговли (в секундах)
            run_intraday: Флаг для запуска внутридневной стратегии
            run_swing: Флаг для запуска свинг-стратегии
        """
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.intraday_interval = intraday_interval
        self.swing_interval = swing_interval
        self.run_intraday = run_intraday
        self.run_swing = run_swing

        # Настройка логирования
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
            handlers=[
                logging.FileHandler('elder_strategy_manager.log', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)

    async def run(self):
        """Запуск всех активированных стратегий"""
        self.logger.info("Запуск менеджера стратегий Elder Triple Screen")

        # Список задач для запуска
        tasks = []

        # Создаем и запускаем стратегии в соответствии с настройками
        if self.run_intraday:
            intraday_strategy = ElderTripleScreenStrategy(
                exchange_id=self.exchange_id,
                check_interval=self.intraday_interval,
                mode='INTRADAY',
                symbol=self.symbol
            )
            tasks.append(intraday_strategy.run_strategy())
            self.logger.info(f"Добавлена стратегия INTRADAY для {self.symbol}")

        if self.run_swing:
            swing_strategy = ElderTripleScreenStrategy(
                exchange_id=self.exchange_id,
                check_interval=self.swing_interval,
                mode='SWING',
                symbol=self.symbol
            )
            tasks.append(swing_strategy.run_strategy())
            self.logger.info(f"Добавлена стратегия SWING для {self.symbol}")

        if not tasks:
            self.logger.warning("Ни одна стратегия не активирована! Завершение работы.")
            return

        # Запускаем все стратегии параллельно
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    # Создаем экземпляр менеджера стратегий с настройками по умолчанию
    manager = ElderStrategyManager(
        exchange_id='binance',
        symbol='BTC/USDT',
        intraday_interval=1200,  # 20 минут для внутридневной торговли
        swing_interval=3600,  # 1 час для свинг-торговли
        run_intraday=True,  # Включаем внутридневную стратегию
        run_swing=True  # Включаем свинг-стратегию
    )

    # Запускаем менеджер
    asyncio.run(manager.run())