import asyncio
import aiohttp
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import sys
import time
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass


@dataclass
class SignalState:
    """Класс для хранения состояния сигнала"""
    time: datetime
    price: float
    type: str
    stop_loss: Optional[float]
    take_profit: Optional[float]
    trailing_activated: bool = False


class ElderTripleScreenStrategy:
    def __init__(self,
                 exchange_id: str = 'binance',
                 check_interval: int = 3600,
                 mode: str = 'INTRADAY'):

        self.timeframes = {
            'SWING': {'trend': '1w', 'entry': '1d', 'intraday': '4h'},
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
        """Анализ тренда с обработкой ошибок"""
        try:
            timeframe = self.timeframes[self.current_mode]['trend']
            df = await self.safe_fetch_ohlcv(symbol, timeframe, self.min_candles['trend'])

            if not self.validate_data(df, self.min_candles['trend'], timeframe):
                return {'trend': 'neutral', 'strength': 'none'}

            df = self.calculate_indicators(df)
            last_hist = df['macd_hist'].iloc[-1]

            trend = 'bullish' if last_hist > 0 else 'bearish'
            return {'trend': trend, 'hist_value': last_hist}

        except Exception as e:
            self.logger.error(f"Ошибка анализа тренда: {str(e)}")
            return {'trend': 'neutral', 'strength': 'none'}

    async def check_entry_signal(self, symbol: str, trend: dict) -> Optional[SignalState]:
        """Поиск точек входа с улучшенными условиями"""
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
                    last['%K'] < 30,
                    last['%K'] > prev['%K'],
                    last['bear_power'] > prev['bear_power'],
                    last['force_index_ema2'] > prev['force_index_ema2'],
                    last['macd_hist'] > prev['macd_hist']
                ]
                signal_type = 'LONG'
            else:
                conditions = [
                    last['%K'] > 70,
                    last['%K'] < prev['%K'],
                    last['bull_power'] < prev['bull_power'],
                    last['force_index_ema2'] < prev['force_index_ema2'],
                    last['macd_hist'] < prev['macd_hist']
                ]
                signal_type = 'SHORT'

            if all(conditions):
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
            self.logger.error(f"Ошибка поиска входа: {str(e)}")
            return None

    def calculate_stop_levels(self, price: float, signal_type: str, df: pd.DataFrame) -> Tuple[float, float]:
        """Расчет стоп-лосса и тейк-профита с использованием ATR"""
        atr_period = 14
        tr = df['high'].combine(df['low'].shift(),
                                lambda x, y: max(x - y, x - df['close'].shift(), df['close'].shift() - y))
        atr = tr.rolling(atr_period).mean().iloc[-1]

        if signal_type == 'LONG':
            stop_loss = price - 2 * atr
            take_profit = price + 3 * atr
        else:
            stop_loss = price + 2 * atr
            take_profit = price - 3 * atr

        return round(stop_loss, 4), round(take_profit, 4)

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

    async def run_strategy(self, symbol: str = 'BTC/USDT'):
        """Основной цикл торговой стратегии"""
        self.logger.info(f"Запуск стратегии для {symbol}")

        while True:
            try:
                # Анализ тренда
                trend = await self.analyze_trend(symbol)

                # Поиск сигнала
                signal = await self.check_entry_signal(symbol, trend)

                if signal:
                    self.active_signals[symbol] = signal
                    self.logger.info(f"Новый сигнал: {signal}")

                # Управление рисками
                await self.manage_risk(symbol)

                # Пауза между итерациями
                await asyncio.sleep(self.check_interval)

            except ccxt.NetworkError as e:
                self.logger.error(f"Ошибка сети: {str(e)}")
                await asyncio.sleep(60)
            except ccxt.ExchangeError as e:
                self.logger.error(f"Ошибка биржи: {str(e)}")
                await asyncio.sleep(300)
            except Exception as e:
                self.logger.error(f"Критическая ошибка: {str(e)}")
                await asyncio.sleep(600)


if __name__ == "__main__":
    strategy = ElderTripleScreenStrategy(mode='INTRADAY')
    asyncio.run(strategy.run_strategy())