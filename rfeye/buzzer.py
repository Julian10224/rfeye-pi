import time
import threading

try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None


class GPIOBuzzer:
    def __init__(self, pin=18, passive=True, active_high=True):
        self.pin = int(pin)
        self.passive = bool(passive)
        self.active_high = bool(active_high)
        self.available = GPIO is not None
        self.pwm = None
        self.lock = threading.Lock()
        self.generation = 0
        if not self.available:
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
            if self.passive:
                self.pwm = GPIO.PWM(self.pin, 1000)
                self.pwm.start(0)
            else:
                GPIO.output(self.pin, GPIO.LOW if self.active_high else GPIO.HIGH)
        except Exception:
            self.available = False
            try:
                GPIO.cleanup(self.pin)
            except Exception:
                pass

    def beep(self, frequency=1000, duration_ms=80, duty=50):
        if not self.available:
            return
        with self.lock:
            self.generation += 1
            token = self.generation
        threading.Thread(target=self._beep_worker,
                         args=(token, int(frequency), int(duration_ms), int(duty)),
                         daemon=True).start()

    def _beep_worker(self, token, frequency, duration_ms, duty):
        try:
            with self.lock:
                if token != self.generation:
                    return
                if self.passive:
                    self.pwm.ChangeFrequency(max(50, frequency))
                    self.pwm.ChangeDutyCycle(max(0, min(100, duty)))
                else:
                    GPIO.output(self.pin, GPIO.HIGH if self.active_high else GPIO.LOW)
            time.sleep(max(0.001, duration_ms / 1000.0))
            with self.lock:
                if token != self.generation:
                    return
                if self.passive:
                    self.pwm.ChangeDutyCycle(0)
                else:
                    GPIO.output(self.pin, GPIO.LOW if self.active_high else GPIO.HIGH)
        except Exception:
            pass

    def off(self):
        if not self.available:
            return
        with self.lock:
            self.generation += 1
            try:
                if self.passive:
                    self.pwm.ChangeDutyCycle(0)
                else:
                    GPIO.output(self.pin, GPIO.LOW if self.active_high else GPIO.HIGH)
            except Exception:
                pass

    def close(self):
        if not self.available:
            return
        try:
            self.off()
            if self.pwm is not None:
                self.pwm.stop()
            GPIO.cleanup(self.pin)
        except Exception:
            pass
