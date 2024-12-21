import asyncio

import aiohttp
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
from typing import List, Dict, Tuple
import time

import requests


class CryptoShakeoutMonitor:
    def __init__(self, 
                 exchange_id: str = 'binance',
                 min_volume: float = 50000,
                 check_interval: int = 3600):  # проверка раз в час
        """
        Инициализация монитора
        
        Args:
            exchange_id: биржа (по умолчанию binance)
            min_volume: минимальный объем торгов за 24ч
            check_interval: интервал проверки в секундах
        """
        # Настройка логирования
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('../crypto_monitor.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        
        # Инициализация параметров
        self.exchange = getattr(ccxt, exchange_id)()
        self.min_volume = min_volume
        self.check_interval = check_interval
        
        # Кеш для данных
        self.signals_cache = {
            'last_signal_time': None,
            'last_signal_price': None
        }
        
    def analyze_weekly_trend(self, symbol: str = 'BTC/USDT') -> str:
        """Анализ недельного тренда"""
        try:
            # Получаем недельные свечи за последние 20 недель
            ohlcv = self.exchange.fetch_ohlcv(symbol, '1w', limit=20)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Рассчитываем EMA
            df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
            
            # Определяем тренд
            current_price = df['close'].iloc[-1]
            ema = df['ema20'].iloc[-1]
            
            if current_price > ema:
                return 'bullish'
            return 'bearish'
            
        except Exception as e:
            self.logger.error(f'Ошибка при анализе недельного тренда: {str(e)}')
            return None

    def detect_shakeout(self, df: pd.DataFrame) -> bool:
        """
        Определение паттерна "встряски" на дневном графике
        """
        try:
            # Рассчитываем индикаторы
            # Bollinger Bands для определения зоны ценности
            df['bb_middle'] = df['close'].rolling(window=20).mean()
            bb_std = df['close'].rolling(window=20).std()
            df['bb_upper'] = df['bb_middle'] + (2 * bb_std)
            df['bb_lower'] = df['bb_middle'] - (2 * bb_std)
            
            # Последние 3 свечи для анализа паттерна
            last_candles = df.iloc[-3:]
            
            # Проверяем условия встряски:
            
            # 1. Резкое падение к зоне ценности или ниже
            price_drop = (last_candles['low'].min() <= last_candles['bb_middle'].iloc[-1])
            
            # 2. Последняя свеча показывает отскок
            last_candle_recovery = (
                last_candles['close'].iloc[-1] > last_candles['open'].iloc[-1] and
                last_candles['close'].iloc[-1] > last_candles['bb_middle'].iloc[-1]
            )
            
            # 3. Объем на отскоке выше среднего
            avg_volume = df['volume'].rolling(window=20).mean().iloc[-1]
            high_volume = last_candles['volume'].iloc[-1] > avg_volume
            
            return price_drop and last_candle_recovery and high_volume
            
        except Exception as e:
            self.logger.error(f'Ошибка при определении встряски: {str(e)}')
            return False

    def analyze_daily_chart(self, symbol: str) -> Dict:
        """Анализ дневного графика"""
        try:
            # Получаем дневные свечи за последние 30 дней
            ohlcv = self.exchange.fetch_ohlcv(symbol, '1d', limit=30)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Проверяем наличие встряски
            shakeout_detected = self.detect_shakeout(df)
            
            if shakeout_detected:
                return {
                    'symbol': symbol,
                    'price': df['close'].iloc[-1],
                    'time': df['timestamp'].iloc[-1],
                    'volume': df['volume'].iloc[-1],
                    'is_signal': True
                }
            self.logger.info(f'Проверяем наличие встряски : False')
            return {'is_signal': False}
            
        except Exception as e:
            self.logger.error(f'Ошибка при анализе дневного графика: {str(e)}')
            return {'is_signal': False}

    def generate_alert_message(self, signal: Dict) -> str:
        """Генерация сообщения для алерта"""
        try:
            if not signal['is_signal']:
                return None
                
            message = f"""
🚨 Обнаружена встряска на {signal['symbol']}!

💰 Текущая цена: {signal['price']:.2f}
📊 Объем за 24ч: {signal['volume']:.2f} USDT
⏰ Время: {signal['time']}

💡 Рекомендация: Проверить график на наличие точки входа
"""
            return message
            
        except Exception as e:
            self.logger.error(f'Ошибка при создании сообщения: {str(e)}')
            return None

    def should_send_signal(self, symbol: str, current_price: float) -> bool:
        """Проверка нужно ли отправлять сигнал"""
        if not self.signals_cache['last_signal_time']:
            return True
            
        # Проверяем прошло ли 24 часа с последнего сигнала
        time_passed = datetime.now() - self.signals_cache['last_signal_time']
        if time_passed.total_seconds() < 24 * 3600:
            return False
            
        # Проверяем изменение цены от последнего сигнала
        if self.signals_cache['last_signal_price']:
            price_change = abs(current_price - self.signals_cache['last_signal_price']) / self.signals_cache['last_signal_price']
            if price_change < 0.02:  # меньше 2% изменения
                return False
                
        return True

    def update_signal_cache(self, symbol: str, price: float):
        """Обновление кеша сигналов"""
        self.signals_cache['last_signal_time'] = datetime.now()
        self.signals_cache['last_signal_price'] = price

    async def run_forever(self, alert_callback=None):
        """
        Основной цикл мониторинга
        
        Args:
            alert_callback: функция для отправки алертов
        """
        self.logger.info('Запуск мониторинга встрясок...')
        
        while True:
            try:
                # 1. Проверяем недельный тренд BTC
                self.logger.info(f'Недельный тренд анализ..')
                weekly_trend = self.analyze_weekly_trend()
                
                if weekly_trend != 'bullish':
                    self.logger.info(f'Недельный тренд {weekly_trend}, пропускаем анализ')
                    time.sleep(self.check_interval)
                    continue
                
                # 2. Анализируем дневной график
                signal = self.analyze_daily_chart('BTC/USDT')
                
                # 3. Если есть сигнал и пришло время его отправить
                if signal['is_signal'] and self.should_send_signal('BTC/USDT', signal['price']):
                    message = self.generate_alert_message(signal)
                    self.logger.info('Обнаружена встряска на BTC')
                    
                    if alert_callback and message:
                        await alert_callback(message)
                        self.update_signal_cache('BTC/USDT', signal['price'])
                
                # Ждем до следующей проверки
                time.sleep(self.check_interval)
                
            except Exception as e:
                self.logger.error(f'Ошибка в основном цикле: {str(e)}')
                time.sleep(60)  # Ждем минуту при ошибке

# Пример использования:

monitor = CryptoShakeoutMonitor(
    exchange_id='binance',
    min_volume=50000,
    check_interval=3600  # проверка раз в час
)

async def send_telegram_alert(message):
    async with aiohttp.ClientSession() as session:
        async with session.post("http://localhost:8000/send_message",
                              json={"text": message}) as response:
            await response.json()

async def main():
    monitor = CryptoShakeoutMonitor(
        exchange_id='binance',
        min_volume=50000,
        check_interval=120
    )
    await monitor.run_forever(alert_callback=send_telegram_alert)

if __name__ == "__main__":
    asyncio.run(main())
