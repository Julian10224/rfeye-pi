import time
import threading

try:
    import pigpio
except Exception:
    pigpio = None

try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None


class GPIOBuzzer:
    """Passive/active buzzer driver with hardware-PWM preference on GPIO18."""

    def __init__(self, pin=18, passive=True, active_high=True):
        self.pin = int(pin)
        self.passive = bool(passive)
        self.active_high = bool(active_high)
        self.lock = threading.Lock()
        self.generation = 0
        self.pwm = None
        self.pi = None
        self.backend = "none"
        self.available = False

        # GPIO18 on a Pi 3B+ supports hardware PWM. pigpio gives much cleaner
        # piezo tones than RPi.GPIO software PWM, especially at 2-3 kHz.
        if self.passive and pigpio is not None:
            try:
                pi = pigpio.pi()
                if pi.connected:
                    self.pi = pi
                    self.pi.set_mode(self.pin, pigpio.OUTPUT)
                    self.pi.hardware_PWM(self.pin, 0, 0)
                    self.backend = "pigpio"
                    self.available = True
                    return
                pi.stop()
            except Exception:
                self.pi = None

        if GPIO is None:
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
            if self.passive:
                self.pwm = GPIO.PWM(self.pin, 2700)
                self.pwm.start(0)
            else:
                GPIO.output(self.pin, GPIO.LOW if self.active_high else GPIO.HIGH)
            self.backend = "rpi_gpio"
            self.available = True
        except Exception:
            self.available = False

    def _tone_on_locked(self, frequency, duty):
        frequency = max(50, int(frequency))
        duty = max(0, min(100, int(duty)))
        if self.passive:
            if self.backend == "pigpio":
                self.pi.hardware_PWM(self.pin, frequency, duty * 10000)
            else:
                self.pwm.ChangeDutyCycle(0)
                self.pwm.ChangeFrequency(frequency)
                self.pwm.ChangeDutyCycle(duty)
        else:
            GPIO.output(self.pin, GPIO.HIGH if self.active_high else GPIO.LOW)

    def _tone_off_locked(self):
        if self.passive:
            if self.backend == "pigpio":
                self.pi.hardware_PWM(self.pin, 0, 0)
            elif self.pwm is not None:
                self.pwm.ChangeDutyCycle(0)
        elif GPIO is not None:
            GPIO.output(self.pin, GPIO.LOW if self.active_high else GPIO.HIGH)

    def beep(self, frequency=2700, duration_ms=30, duty=50):
        if not self.available:
            return
        with self.lock:
            self.generation += 1
            token = self.generation
            self._tone_off_locked()
            self._tone_on_locked(frequency, duty)
        threading.Thread(
            target=self._finish_beep,
            args=(token, max(1, int(duration_ms))),
            daemon=True,
        ).start()

    def _finish_beep(self, token, duration_ms):
        time.sleep(duration_ms / 1000.0)
        try:
            with self.lock:
                if token == self.generation:
                    self._tone_off_locked()
        except Exception:
            pass

    def off(self):
        if not self.available:
            return
        with self.lock:
            self.generation += 1
            try:
                self._tone_off_locked()
            except Exception:
                pass

    def close(self):
        if not self.available:
            return
        try:
            self.off()
            if self.pwm is not None:
                self.pwm.stop()
            if self.pi is not None:
                self.pi.stop()
            if self.backend == "rpi_gpio" and GPIO is not None:
                GPIO.cleanup(self.pin)
        except Exception:
            pass
