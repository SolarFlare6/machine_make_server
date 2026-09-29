"""
robot_controller.py

Example Robot Controller for a quadruped
(device_server_hardware_mapper.txt, sections 33-34;
dcp_protocol_specification.txt, section 37;
MachineMake Server Implementation Specification DCP v1.0).

This is where DCP tool calls interface with hardware/simulated hardware.
Per user specification: simulated hardware functions with console prints
confirming execution, maintaining full API compatibility with the mobile app.
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional


class RobotController:
    def __init__(self, physical_config: Dict[str, Any], hardware_map: Optional[Dict[str, Any]] = None):
        self._servos = physical_config.get("servos", {})
        self._pca9685_cfg = physical_config.get("pca9685", {})
        self.hardware_map: Dict[str, Any] = hardware_map or {}
        self.standing = False
        self.orientation = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
        self.height = 0.0
        self.led_state = False
        self.gait = "walk"
        self.camera_streaming = False
        self.dynamic_config: Dict[str, Any] = {}
        self.gpio_pins: Dict[int, bool] = {}
        self.pwm_channels: Dict[int, float] = {}

        # Simulated 16-channel PCA9685 servo positions (0-15, angles 0-180 deg or None if de-energized)
        self.servo_angles: Dict[int, Optional[float]] = {i: None for i in range(16)}

        # Simulated WS281x 8-LED strip (GPIO 10)
        self.strip_leds: List[Dict[str, int]] = [{"r": 0, "g": 0, "b": 0} for _ in range(8)]

        # Simulated buzzer (GPIO 23)
        self.current_tone: Optional[str] = None

    def set_hardware_map(self, hardware_map: Dict[str, Any]) -> None:
        """Stores the hardware scan result for Needle AI awareness."""
        self.hardware_map = hardware_map or {}

    # -- simulated low-level hardware I/O --------------------------------

    async def _drive_servos(self, positions: Dict[str, float], duration_s: float) -> None:
        """Stand-in for real servo motion. `positions` maps servo name ->
        target angle; a real implementation would compute inverse
        kinematics and stream PWM updates to the PCA9685 here."""
        await asyncio.sleep(duration_s)

    # -- Section 3.1: Quadruped Locomotion & High-Level Actions ----------

    async def stand(self, progress_cb: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        """Make the robot stand up using calibrated standing angles."""
        print("[RobotController] stand() called")
        await self._drive_servos({name: 90 for name in self._servos}, duration_s=0.1)
        self.standing = True
        # Calibrate standing angles across PCA9685 channels
        for i in (3, 4, 6, 7, 9, 10, 12, 13):
            self.servo_angles[i] = 90.0
        return {"standing": True, "status": "standing"}

    async def sit(self, progress_cb: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        """Make the robot sit down using calibrated resting angles."""
        print("[RobotController] sit() called")
        await self._drive_servos({name: 0 for name in self._servos}, duration_s=0.1)
        self.standing = False
        for i in (3, 4, 6, 7, 9, 10, 12, 13):
            self.servo_angles[i] = 0.0
        return {"standing": False, "status": "sitting"}

    async def walk(self, direction: str, distance: float,
                   progress_cb: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        """Executes forward/backward/lateral step cycles."""
        print(f"[RobotController] walk() called with direction={direction}, distance={distance}")
        steps = 5
        for step in range(1, steps + 1):
            await self._drive_servos({}, duration_s=0.05)
            if progress_cb:
                progress_cb(step / steps)
        return {"direction": direction, "distance": float(distance), "status": "completed"}

    async def turn(self, direction: str, angle: float,
                   progress_cb: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        """Rotates robot body by specified degrees."""
        print(f"[RobotController] turn() called with direction={direction}, angle={angle}")
        steps = 3
        for step in range(1, steps + 1):
            await self._drive_servos({}, duration_s=0.05)
            if progress_cb:
                progress_cb(step / steps)
        return {"direction": direction, "angle": float(angle), "status": "completed"}

    async def emergency_stop(self) -> Dict[str, Any]:
        """Immediately halts all motion, disables servo drive, and sits down."""
        print("[RobotController] emergency_stop() called (halting locomotion, cutting servo power)")
        self.standing = False
        for i in range(16):
            self.servo_angles[i] = None
        await self._drive_servos({name: 0 for name in self._servos}, duration_s=0.01)
        return {"stopped": True, "standing": False, "message": "Emergency stop triggered"}

    # -- Section 3.2: Direct Servo Control (PCA9685 16-Channel) -----------

    async def driver_set_servo_angle_with_index(self, index: int, angle: float) -> Dict[str, Any]:
        """Sets angle of single servo channel (0-15, 0-180)."""
        idx = int(index)
        clamped_angle = max(0.0, min(180.0, float(angle)))
        if 0 <= idx < 16:
            self.servo_angles[idx] = clamped_angle
        print(f"[RobotController] driver_set_servo_angle_with_index() called with index={idx}, angle={clamped_angle}")
        return {"index": idx, "angle": clamped_angle, "status": "success"}

    async def snap_servo_left(self, index: int) -> Dict[str, Any]:
        """Sets channel angle to 180 deg immediately."""
        idx = int(index)
        if 0 <= idx < 16:
            self.servo_angles[idx] = 180.0
        print(f"[RobotController] snap_servo_left() called with index={idx}")
        return {"index": idx, "angle": 180.0, "status": "snapped_left"}

    async def snap_servo_right(self, index: int) -> Dict[str, Any]:
        """Sets channel angle to 0 deg immediately."""
        idx = int(index)
        if 0 <= idx < 16:
            self.servo_angles[idx] = 0.0
        print(f"[RobotController] snap_servo_right() called with index={idx}")
        return {"index": idx, "angle": 0.0, "status": "snapped_right"}

    async def pan_to_left(self, index: int) -> Dict[str, Any]:
        """Sweeps channel smoothly towards 180 deg."""
        idx = int(index)
        if 0 <= idx < 16:
            self.servo_angles[idx] = 180.0
        print(f"[RobotController] pan_to_left() called with index={idx}")
        return {"index": idx, "angle": 180.0, "status": "panned_left"}

    async def pan_to_right(self, index: int) -> Dict[str, Any]:
        """Sweeps channel smoothly towards 0 deg."""
        idx = int(index)
        if 0 <= idx < 16:
            self.servo_angles[idx] = 0.0
        print(f"[RobotController] pan_to_right() called with index={idx}")
        return {"index": idx, "angle": 0.0, "status": "panned_right"}

    async def cleanup_servos(self) -> Dict[str, Any]:
        """Critical for Halt: Sets servo angle to None across all 16 channels to de-energize coils."""
        self.standing = False
        for i in range(16):
            self.servo_angles[i] = None
        print("[RobotController] cleanup_servos() called: all 16 servos de-energized and coils released")
        return {"status": "servos_released", "released": True}

    # -- Section 3.3: WS281x 8-LED Strip Controls (GPIO 10) ---------------

    async def turn_on_strip_with_color(self, r: int = 255, g: int = 255, b: int = 255) -> Dict[str, Any]:
        """Fills all 8 LEDs with RGB color."""
        r_c, g_c, b_c = max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))
        self.strip_leds = [{"r": r_c, "g": g_c, "b": b_c} for _ in range(8)]
        print(f"[RobotController] turn_on_strip_with_color() called with r={r_c}, g={g_c}, b={b_c}")
        return {"color": [r_c, g_c, b_c], "status": "success"}

    async def turn_off_strip(self) -> Dict[str, Any]:
        """Turns off all 8 LEDs."""
        self.strip_leds = [{"r": 0, "g": 0, "b": 0} for _ in range(8)]
        print("[RobotController] turn_off_strip() called")
        return {"status": "off"}

    async def turn_on_led_at_index(self, index: int = 0, r: int = 255, g: int = 255, b: int = 255) -> Dict[str, Any]:
        """Sets single LED color at index 0-7."""
        idx = int(index)
        r_c, g_c, b_c = max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))
        if 0 <= idx < len(self.strip_leds):
            self.strip_leds[idx] = {"r": r_c, "g": g_c, "b": b_c}
        print(f"[RobotController] turn_on_led_at_index() called with index={idx}, r={r_c}, g={g_c}, b={b_c}")
        return {"index": idx, "color": [r_c, g_c, b_c], "status": "success"}

    async def turn_off_led_at_index(self, index: int = 0) -> Dict[str, Any]:
        """Turns off single LED at index 0-7."""
        idx = int(index)
        if 0 <= idx < len(self.strip_leds):
            self.strip_leds[idx] = {"r": 0, "g": 0, "b": 0}
        print(f"[RobotController] turn_off_led_at_index() called with index={idx}")
        return {"index": idx, "status": "off"}

    async def animate_running_process(self) -> Dict[str, Any]:
        """Runs animated LED chase / cycle."""
        print("[RobotController] animate_running_process() called")
        return {"status": "running", "animation": "running_process"}

    async def flash_alert(self) -> Dict[str, Any]:
        """Rapid red blinking alert sequence."""
        print("[RobotController] flash_alert() called")
        return {"status": "alert", "animation": "flash_alert"}

    async def blink_oke(self) -> Dict[str, Any]:
        """Double green flash confirming command success."""
        print("[RobotController] blink_oke() called")
        return {"status": "ok", "animation": "blink_oke"}

    async def blink_warning(self) -> Dict[str, Any]:
        """Amber pulsing warning sequence."""
        print("[RobotController] blink_warning() called")
        return {"status": "warning", "animation": "blink_warning"}

    # -- Section 3.4: Piezo Buzzer Controls (GPIO 23) ---------------------

    async def play_tone(self, tone: str = "C4") -> Dict[str, Any]:
        """Plays frequency using tone generator."""
        self.current_tone = str(tone)
        print(f"[RobotController] play_tone() called with tone={tone}")
        return {"status": "playing", "tone": str(tone)}

    async def play_list_of_notes(self, notes: Any = None) -> Dict[str, Any]:
        """Plays melody sequence asynchronously."""
        note_list = list(notes) if isinstance(notes, (list, tuple)) else [str(notes or "C4")]
        print(f"[RobotController] play_list_of_notes() called with notes={note_list}")
        return {"status": "playing_melody", "notes": note_list}

    # -- Section 3.5: MPU6050 IMU Telemetry (I2C 0x68) -------------------

    async def get_sensor_accel(self) -> Dict[str, float]:
        """Returns MPU6050 accelerometer readings in g."""
        print("[RobotController] get_sensor_accel() called")
        return {"x": 0.01, "y": -0.04, "z": 0.99}

    async def get_sensor_gyro(self) -> Dict[str, float]:
        """Returns MPU6050 gyroscope readings in deg/s."""
        print("[RobotController] get_sensor_gyro() called")
        return {"x": 0.2, "y": -0.5, "z": 0.1}

    async def get_pitch_roll(self) -> Dict[str, float]:
        """Returns MPU6050 pitch and roll calculated via math.atan2."""
        print("[RobotController] get_pitch_roll() called")
        return {"pitch": 0.42, "roll": -1.15}

    def get_mpu6050_telemetry(self) -> Dict[str, Any]:
        """Helper returning full IMU telemetry structure for periodic broadcast."""
        return {
            "pitch": 0.42,
            "roll": -1.15,
            "yaw": 0.0,
            "accel": {"x": 0.01, "y": -0.04, "z": 0.99},
            "gyro": {"x": 0.2, "y": -0.5, "z": 0.1},
        }

    # -- Section 3.6: System & Power Options ------------------------------

    async def shutdown(self) -> Dict[str, Any]:
        """Simulated sudo shutdown -h now."""
        print("[RobotController] shutdown() called (simulating sudo shutdown -h now)")
        return {"status": "shutting_down", "action": "shutdown"}

    async def reboot(self) -> Dict[str, Any]:
        """Simulated sudo reboot."""
        print("[RobotController] reboot() called (simulating sudo reboot)")
        return {"status": "rebooting", "action": "reboot"}

    # -- Kinematics, Gait, Camera, Config & Telemetry ---------------------

    async def set_gait(self, mode: str = "walk") -> Dict[str, Any]:
        """Sets the walking gait mode (walk, trot, bound, gallop)."""
        valid_modes = ["walk", "trot", "bound", "gallop"]
        selected = str(mode).lower() if str(mode).lower() in valid_modes else "walk"
        self.gait = selected
        print(f"[RobotController] set_gait() called with mode={selected}")
        return {"gait": selected, "supported_gaits": valid_modes}

    async def set_pose(self, pitch: float = 0.0, roll: float = 0.0,
                       yaw: float = 0.0, height: float = 0.0) -> Dict[str, Any]:
        """Sets body orientation (kinematic pose) and body height."""
        self.orientation = {"roll": float(roll), "pitch": float(pitch), "yaw": float(yaw)}
        self.height = float(height)
        print(f"[RobotController] set_pose() called with pitch={pitch}, roll={roll}, yaw={yaw}, height={height}")
        return {"orientation": dict(self.orientation), "height": self.height}

    async def start_camera(self, resolution: str = "640x480", fps: int = 30) -> Dict[str, Any]:
        """Starts camera stream endpoint."""
        self.camera_streaming = True
        print(f"[RobotController] start_camera() called with resolution={resolution}, fps={fps}")
        return {"streaming": True, "resolution": resolution, "fps": fps, "stream_path": "/camera/stream"}

    async def stop_camera(self) -> Dict[str, Any]:
        """Stops camera stream endpoint."""
        self.camera_streaming = False
        print("[RobotController] stop_camera() called")
        return {"streaming": False}

    async def camera_snapshot(self) -> Dict[str, Any]:
        """Alias for take_picture used by the mobile app."""
        print("[RobotController] camera_snapshot() called")
        return await self.take_picture()

    async def take_picture(self) -> Dict[str, Any]:
        """Simulated camera capture."""
        print("[RobotController] take_picture() called")
        return {"format": "jpeg", "bytes": 0, "note": "simulated capture"}

    async def get_battery(self) -> Dict[str, Any]:
        """Returns battery telemetry."""
        print("[RobotController] get_battery() called")
        return {
            "percentage": 88,
            "voltage": 8.1,
            "charging": False,
            "level": 88,
        }

    async def set_config(self, param: str, value: Any = None) -> Dict[str, Any]:
        """Updates a dynamic runtime parameter."""
        self.dynamic_config[param] = value
        print(f"[RobotController] set_config() called with param={param}, value={value}")
        return {"updated": True, "param": param, "value": value}

    async def get_orientation(self) -> Dict[str, float]:
        """Reads IMU orientation."""
        print("[RobotController] get_orientation() called")
        return dict(self.orientation)

    async def set_led(self, on: bool) -> Dict[str, Any]:
        """Sets simple status LED state."""
        self.led_state = bool(on)
        print(f"[RobotController] set_led() called with on={on}")
        return {"status": "success", "led_state": self.led_state}

    # -- Needle AI on-device reasoning -----------------------------------

    async def needle_prompt(self, prompt: str) -> Dict[str, Any]:
        """Interprets a natural language command on-device and invokes the corresponding action."""
        print(f"[RobotController] needle_prompt() called with prompt='{prompt}'")
        text = (prompt or "").lower().strip()

        if "stand" in text:
            await self.stand()
            return {"status": "executed", "action": "stand", "message": "Robot is standing"}
        elif "sit" in text:
            await self.sit()
            return {"status": "executed", "action": "sit", "message": "Robot is sitting"}
        elif "stop" in text or "halt" in text:
            return await self.emergency_stop()
        elif "light" in text or "led" in text or "flashlight" in text:
            turn_on = not ("off" in text or "kill" in text)
            await self.set_led(on=turn_on)
            return {"status": "executed", "action": "set_led", "message": f"LED turned {'on' if turn_on else 'off'}"}
        elif "walk" in text or "forward" in text or "backward" in text:
            direction = "backward" if "back" in text else ("left" if "left" in text else ("right" if "right" in text else "forward"))
            import re
            match = re.search(r'(\d+(?:\.\d+)?)', text)
            dist = float(match.group(1)) if match else 1.0
            await self.walk(direction=direction, distance=dist)
            return {"status": "executed", "action": "walk", "message": f"Walked {direction} {dist}m"}
        elif "turn" in text or "rotate" in text:
            direction = "right" if "right" in text else "left"
            import re
            match = re.search(r'(\d+(?:\.\d+)?)', text)
            angle = float(match.group(1)) if match else 45.0
            await self.turn(direction=direction, angle=angle)
            return {"status": "executed", "action": "turn", "message": f"Turned {direction} {angle} deg"}
        elif any(k in text for k in ("hardware", "spec", "system info", "what are you", "what hardware", "device info")):
            dev = self.hardware_map.get("device", {})
            sys_info = self.hardware_map.get("system", {})
            active_ifaces = [k for k, v in self.hardware_map.get("interfaces", {}).items() if v]
            platform_str = dev.get("platform", "unknown")
            os_str = dev.get("os", "unknown")
            arch_str = dev.get("architecture", "")
            cores = sys_info.get("cpu", {}).get("cores", "unknown")
            msg = f"Host: {platform_str} ({os_str} {arch_str}, {cores} cores). Active interfaces: {', '.join(active_ifaces) or 'none detected'}."
            return {
                "status": "executed",
                "action": "hardware_info",
                "message": msg,
                "hardware_map": self.hardware_map,
            }

        return {
            "status": "unrecognized",
            "message": f"Needle AI could not interpret prompt: '{prompt}'",
        }

    # -- Generic Hardware Control (GPIO & PWM) --------------------------

    async def gpio_write(self, pin: int, state: bool) -> Dict[str, Any]:
        """Sets digital state (HIGH/LOW) of a GPIO pin."""
        pin = int(pin)
        state = bool(state)
        self.gpio_pins[pin] = state
        print(f"[RobotController] gpio_write() called with pin={pin}, state={state}")
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)
        except Exception:
            pass  # Simulation or running without physical RPi GPIO
        return {"pin": pin, "state": state, "status": "success"}

    async def gpio_read(self, pin: int) -> Dict[str, Any]:
        """Reads digital state of a GPIO pin."""
        pin = int(pin)
        val = self.gpio_pins.get(pin, False)
        print(f"[RobotController] gpio_read() called with pin={pin}")
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.IN)
            val = bool(GPIO.input(pin))
        except Exception:
            pass
        return {"pin": pin, "state": val}

    async def pwm_set(self, channel: int, value: float) -> Dict[str, Any]:
        """Sets duty cycle on a PWM channel (0.0 to 1.0)."""
        channel = int(channel)
        clamped = max(0.0, min(1.0, float(value)))
        self.pwm_channels[channel] = clamped
        print(f"[RobotController] pwm_set() called with channel={channel}, value={clamped}")
        return {"channel": channel, "value": clamped, "status": "success"}
