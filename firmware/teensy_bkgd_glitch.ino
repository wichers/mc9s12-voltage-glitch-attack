/*
 * MC9S12D64 BDM Security Bypass — Voltage Glitcher
 *
 * Teensy 4.x firmware with two glitch trigger modes:
 *
 * 1. RESET mode (R command): fires glitch at configurable delay after
 *    RESET rising edge. Used to corrupt the BDM ROM's one-time security
 *    byte evaluation during boot. If successful, UNSEC=1 and all
 *    subsequent BDM reads return real flash data.
 *
 * 2. BKGD edge mode (A/C commands): counts 24 falling edges on BKGD
 *    (one READ_WORD command), then fires glitch. Legacy mode.
 *
 * Hardware (matches glitchsink.ino.org pin layout):
 *   Pin 2  -> 74HC4053 INH (MUX_ENABLE_PIN)  — LOW=enabled, HIGH=output disabled
 *   Pin 3  -> 74HC4053 select A/B/C (MUX_CONTROL_PIN) — LOW=normal VCC, HIGH=glitch VCC
 *   Pin 6  -> BKGD wire tap (high-impedance input, moved off pin 2)
 *   Pin 12 -> Target RESET (input, monitor)
 *   Pin 13 -> LED
 *
 * Serial commands (115200 baud):
 *   D<ns>\n  - Set glitch delay (0-15000000 ns, i.e. up to 15ms)
 *   W<ns>\n  - Set glitch pulse width (0-50000 ns)
 *   R\n      - Arm RESET mode (fire on next RESET rising edge)
 *   E<n>\n   - Set edge count (1-255, default 24). Use E1 with LED-sync wiring.
 *   A\n      - Arm BKGD edge mode (single-shot, N edges)
 *   C\n      - Arm BKGD edge mode (continuous)
 *   Q<ns>\n  - Auto-sweep step (ns). Delay increments by <ns> after each glitch.
 *             Wraps at 200000ns. 0=disable. Prints HIT when edge gap >200ms.
 *   P<us>\n  - Periodic free-run mode: fire glitch every <us> microseconds (0=stop)
 *   X\n      - Disarm (emergency stop, also stops sweep/periodic)
 *   S\n      - Status report
 *   T\n      - Timing calibration (passive BKGD edge measurement)
 *
 * Reports:
 *   R<delay_ns>,<width_ns>\n  - Reset-triggered glitch fired
 *   G<edges>,<delay_ns>,<width_ns>,<bit_ns>\n  - BKGD glitch fired
 *   F<period_us>,<width_ns>,<total>\n          - Periodic glitch fired (every Nth, throttled)
 *   T<bit_ns>,<bus_khz>,<window_ns>\n          - Timing measurement
 */

#define MUX_ENABLE_PIN   2    // 74HC4053 INH (OUTPUT) — LOW=enabled, HIGH=disabled
#define MUX_CONTROL_PIN  3    // 74HC4053 select A/B/C (OUTPUT) — LOW=normal VCC, HIGH=glitch VCC
#define BKGD_PIN         6    // BKGD wire tap (INPUT, high-Z) — moved off pin 2
#define RESET_PIN       12    // Target RESET (INPUT, monitor only)
#define LED_PIN         13

// Mux control macros (matching glitchsink.ino.org convention)
#define v_normal()    digitalWriteFast(MUX_CONTROL_PIN, HIGH)
#define v_glitch()    digitalWriteFast(MUX_CONTROL_PIN, LOW)
#define enable_mux()  digitalWriteFast(MUX_ENABLE_PIN, LOW)
#define disable_mux() digitalWriteFast(MUX_ENABLE_PIN, HIGH)

// Number of falling edges before firing glitch — runtime configurable via E command.
// Default 24: READ_WORD (0xE8) = 8 (opcode) + 8 (addr_hi) + 8 (addr_lo) bits.
// Set to 1 when using target LED output as sync signal (LED-sync mode).
static uint32_t edgeTarget = 24;

// If no falling edge for this many CPU cycles, consider command done.
// At 2MHz bus clock, bit period = 8us. Inter-command gap is >>100us.
// 200us at 600MHz = 120,000 cycles. Use 300us for safety.
#define IDLE_TIMEOUT_CYCLES  (600UL * 300UL)  // 300us

// Convert nanoseconds to Teensy 4.x CPU cycles (600MHz)
// 600 cycles = 1000ns, so cycles = ns * 6 / 10
static inline uint32_t nsToCycles(uint32_t ns) {
    return (uint32_t)((uint64_t)ns * 6ULL / 10ULL);
}

// State machine
enum State : uint8_t {
    STATE_IDLE,       // Not armed, ignoring BKGD edges
    STATE_ARMED,      // Waiting for first falling edge
    STATE_COUNTING,   // Counting falling edges (1..24)
    STATE_GLITCHING,  // Busy-wait + fire (runs in ISR context)
    STATE_COOLDOWN,   // Waiting for idle gap before re-arm
    STATE_CALIBRATE,  // Passive: measure timing without firing glitch
    STATE_RESET_ARMED // Waiting for RESET rising edge to fire glitch
};

static volatile State state = STATE_IDLE;
static volatile uint32_t edgeCount = 0;
static volatile uint32_t lastEdgeCycles = 0;
static volatile uint32_t firstEdgeCycles = 0;   // timestamp of edge #1
static volatile uint32_t edge24Cycles = 0;
static volatile bool glitchFired = false;
static volatile bool continuous = false;
static volatile uint32_t glitchesTotal = 0;
static volatile bool hitEdgeDetected = false;  // set by ISR when target LED blinks (P-mode hit)
static volatile bool resetModeActive = false;  // true when R command armed, false on X or auto-disarm

// Timing measurement (updated after every 24-edge sequence)
static volatile uint32_t measuredBitCycles = 0;  // avg cycles between edges
static volatile bool timingReady = false;         // new measurement available

// Configurable parameters
// Default delay: 8000ns = 1 bit period (8us) past 24th edge,
// which is approximately the start of the 150-bus-clock window.
static uint32_t glitchDelayNs = 8000;
static uint32_t glitchWidthNs = 20;

// Auto-sweep mode (Q command) — after each glitch cycle, delay increments
// automatically by sweepStepNs until sweepMaxNs, then wraps to 0.
// Hit detection: if BKGD edges disappear for >200ms while armed, print HIT.
static uint32_t sweepStepNs = 0;       // 0 = disabled; set via Q command
static uint32_t sweepMaxNs  = 200000;  // default max: covers 200µs window

// Periodic free-run mode (P command) — fires glitch at fixed interval,
// independent of BKGD edges. Used when target's sensitive window is not
// synchronized with BDM traffic (e.g. 25MHz standalone test target).
static IntervalTimer periodicTimer;
static volatile uint32_t periodicPeriodUs = 0;  // 0 = disabled
static volatile uint32_t periodicCount = 0;
static volatile uint32_t periodicHits  = 0;   // incremented on each P-mode hit
static volatile bool resetDetected = false;   // set by ISR when target RESET pin goes LOW

// IntervalTimer ISR — fires at periodicPeriodUs interval
static void periodicISR() {
    uint32_t widthCycles = nsToCycles(glitchWidthNs);
    v_glitch();
    uint32_t t = ARM_DWT_CYCCNT + widthCycles;
    while ((int32_t)(ARM_DWT_CYCCNT - t) < 0) {}
    v_normal();
    periodicCount++;
    // Print every 1000 glitches so the user sees it's running without flooding serial
    if (periodicCount % 1000 == 0) {
        glitchFired = true;  // reuse flag; loop() will print F line
    }
}

// ISR: called when target RESET pin goes LOW (active-low RESET asserted)
static void resetISR() { resetDetected = true; }

// Forward declarations
static void bkgdISR();
static void fireGlitch();
static void handleSerial();
static void printStatus();
static void periodicISR();
static void resetRisingISR();

// ISR: called on RESET rising edge (reset released) when R-mode armed.
// Fires glitch at configurable delay after RESET release — targets
// the BDM ROM's one-time security byte evaluation during boot.
static void resetRisingISR() {
    if (state != STATE_RESET_ARMED) return;
    edge24Cycles = ARM_DWT_CYCCNT;  // reuse as "trigger moment" for fireGlitch()
    state = STATE_GLITCHING;
    fireGlitch();                   // delay + width busy-wait, sets glitchFired
    state = STATE_IDLE;
}

void setup() {
    // Enable ARM cycle counter for precise timing
    ARM_DEMCR |= ARM_DEMCR_TRCENA;
    ARM_DWT_CTRL |= ARM_DWT_CTRL_CYCCNTENA;

    // Mux: select normal VCC, enable output
    pinMode(MUX_CONTROL_PIN, OUTPUT);
    pinMode(MUX_ENABLE_PIN, OUTPUT);
    v_normal();    // LOW on MUX_CONTROL_PIN → normal VCC selected
    enable_mux();  // LOW on MUX_ENABLE_PIN → INH=0, mux output active

    pinMode(BKGD_PIN, INPUT);
    pinMode(RESET_PIN, INPUT);
    pinMode(LED_PIN, OUTPUT);

    Serial.begin(115200);
    delay(500);

    Serial.println("=== BKGD Edge-Counting Glitcher ===");
    Serial.printf("Target: MC9S12D64, 2MHz bus clock (4MHz osc / 2)\n");
    Serial.printf("BDM bit period: 8us, Security window: 75us (150 bus clocks)\n");
    Serial.printf("Edge target: %lu (E1=LED-sync, E24=BDM READ_WORD)\n", edgeTarget);
    Serial.printf("Pins: BKGD=%d, MuxCtrl=%d, MuxEnable=%d, Reset=%d\n",
                  BKGD_PIN, MUX_CONTROL_PIN, MUX_ENABLE_PIN, RESET_PIN);
    Serial.printf("Delay: %lu ns, Width: %lu ns\n", glitchDelayNs, glitchWidthNs);
    Serial.println("Commands: D<ns> W<ns> E<n> R A C Q<ns> P<us> X S T");

    // Attach falling-edge interrupt on BKGD pin
    attachInterrupt(digitalPinToInterrupt(BKGD_PIN), bkgdISR, FALLING);
    // Monitor target RESET: FALLING = RESET asserted (active-low)
    attachInterrupt(digitalPinToInterrupt(RESET_PIN), resetISR, FALLING);
}

// Helper: convert measured bit cycles to nanoseconds and derived values
static uint32_t bitPeriodNs() {
    return (uint32_t)((uint64_t)measuredBitCycles * 10ULL / 6ULL);
}

static uint32_t busClockKhz() {
    // bit period = 16 bus clocks → bus_freq = 16 / bit_period
    // In kHz: 16,000,000 / bit_period_ns
    uint32_t bp = bitPeriodNs();
    return (bp > 0) ? (16000000UL / bp) : 0;
}

static uint32_t securityWindowNs() {
    // 150 bus clocks = 150/16 * bit_period_ns = 9.375 * bit_period_ns
    uint32_t bp = bitPeriodNs();
    return (uint32_t)((uint64_t)bp * 150ULL / 16ULL);
}

void loop() {
    // Check if ISR signaled a successful glitch
    if (glitchFired) {
        glitchFired = false;
        digitalWriteFast(LED_PIN, HIGH);

        if (resetModeActive) {
            // RESET mode: single-shot glitch after RESET rising edge
            Serial.printf("R%lu,%lu\n", glitchDelayNs, glitchWidthNs);
            // Auto-disarm: restore RESET_PIN to FALLING monitor
            resetModeActive = false;
            detachInterrupt(digitalPinToInterrupt(RESET_PIN));
            attachInterrupt(digitalPinToInterrupt(RESET_PIN), resetISR, FALLING);
        } else if (periodicPeriodUs > 0) {
            // Periodic mode: dot = no event, X = RESET seen since last checkpoint
            if (resetDetected) {
                resetDetected = false;
                Serial.print('X');
            } else {
                Serial.print('.');
            }
        } else {
            // BKGD edge mode: report edges, delay, width, bit period
            Serial.printf("G%lu,%lu,%lu,%lu\n",
                          (uint32_t)edgeCount, glitchDelayNs, glitchWidthNs,
                          bitPeriodNs());

            if (continuous) {
                // Advance sweep delay before re-arming
                if (sweepStepNs > 0) {
                    glitchDelayNs += sweepStepNs;
                    if (glitchDelayNs > sweepMaxNs) glitchDelayNs = 0;
                    Serial.printf("SWEEP D=%lu W=%lu\n", glitchDelayNs, glitchWidthNs);
                }
                if (edgeTarget == 1) {
                    // Single-edge (LED-sync) mode: skip COOLDOWN.
                    // Target pulses every ~128µs — the 300µs COOLDOWN timeout
                    // would never expire before the next edge arrives.
                    edgeCount = 0;
                    state = STATE_ARMED;
                } else {
                    state = STATE_COOLDOWN;
                }
            } else {
                state = STATE_IDLE;
            }
        }

        delayMicroseconds(500);
        digitalWriteFast(LED_PIN, LOW);
    }

    // P-mode hit detection: bkgdISR set hitEdgeDetected when target LED blinked
    if (hitEdgeDetected) {
        hitEdgeDetected = false;
        if (periodicPeriodUs > 0) {
            periodicHits++;
            // \n ensures HIT appears on its own line (dots have no newline)
            Serial.printf("\nHIT! (P-mode) W=%lu total=%lu hits=%lu\n",
                          glitchWidthNs, periodicCount, periodicHits);
            // Hold Teensy LED on for 600ms so user can observe target's 3×100ms flash
            digitalWriteFast(LED_PIN, HIGH);
            delay(600);
            digitalWriteFast(LED_PIN, LOW);
        }
    }

    // Check if calibration produced a timing result
    if (timingReady) {
        timingReady = false;
        uint32_t bp = bitPeriodNs();
        uint32_t bk = busClockKhz();
        uint32_t wn = securityWindowNs();
        Serial.printf("T%lu,%lu,%lu\n", bp, bk, wn);
        Serial.printf("  Bit period:   %lu ns (expected 8000 for 2MHz)\n", bp);
        Serial.printf("  Bus clock:    %lu kHz (expected 2000)\n", bk);
        Serial.printf("  150-clk window: %lu ns (this is your sweep range)\n", wn);
    }

    // Cooldown -> re-arm after idle gap
    if (state == STATE_COOLDOWN) {
        uint32_t now = ARM_DWT_CYCCNT;
        if ((now - lastEdgeCycles) > IDLE_TIMEOUT_CYCLES) {
            edgeCount = 0;
            state = STATE_ARMED;
        }
    }

    // Timeout during counting: command wasn't N edges, or we missed some.
    // Reset and stay armed.
    if (state == STATE_COUNTING) {
        uint32_t now = ARM_DWT_CYCCNT;
        if ((now - lastEdgeCycles) > IDLE_TIMEOUT_CYCLES) {
            edgeCount = 0;
            state = STATE_ARMED;
        }
    }

    // Hit detection: if edges disappear for >200ms while running, the target
    // has entered its flash sequence (3×100ms = 600ms silence). Print HIT.
    // Guard on glitchesTotal > 0: can't have a hit before any glitch fired
    // (prevents false positive when target LED is not yet connected/sending pulses).
    if ((continuous || sweepStepNs > 0) && state == STATE_ARMED && glitchesTotal > 0) {
        uint32_t sinceLastMs = (ARM_DWT_CYCCNT - lastEdgeCycles) / 600000UL;
        if (sinceLastMs > 200) {
            Serial.printf("HIT! D=%lu W=%lu total=%lu\n",
                          glitchDelayNs, glitchWidthNs, (uint32_t)glitchesTotal);
            digitalWriteFast(LED_PIN, HIGH);
            sweepStepNs = 0;
            continuous = false;
            state = STATE_IDLE;
            periodicTimer.end();
            periodicPeriodUs = 0;
            v_normal();
            delay(2000);  // keep LED on for 2s so user notices
            digitalWriteFast(LED_PIN, LOW);
        }
    }

    handleSerial();
}

// ISR: called on every BKGD falling edge
// Runs with interrupts disabled (Cortex-M7 default), giving us
// exclusive CPU for the busy-wait glitch timing.
static void bkgdISR() {
    uint32_t now = ARM_DWT_CYCCNT;

    if (state == STATE_ARMED) {
        // First falling edge of a new command sequence
        edgeCount = 1;
        firstEdgeCycles = now;
        lastEdgeCycles = now;
        edge24Cycles = now;   // always record timestamp; used by fireGlitch()
        if (edgeTarget == 1) {
            // Single-edge mode: fire immediately on first edge
            state = STATE_GLITCHING;
            fireGlitch();
            return;
        }
        state = STATE_COUNTING;
        return;
    }

    if (state == STATE_COUNTING) {
        // Check for idle gap mid-count (means a new command started,
        // previous count was from a different/shorter command)
        if ((now - lastEdgeCycles) > IDLE_TIMEOUT_CYCLES) {
            edgeCount = 1;
            firstEdgeCycles = now;
            lastEdgeCycles = now;
            return;
        }

        edgeCount++;
        lastEdgeCycles = now;

        if (edgeCount >= edgeTarget) {
            // Nth falling edge reached. Compute measured bit period if possible.
            // Average over (edgeTarget-1) intervals: only valid for edgeTarget > 1.
            edge24Cycles = now;
            if (edgeTarget > 1)
                measuredBitCycles = (now - firstEdgeCycles) / (edgeTarget - 1);

            // Fire the glitch
            state = STATE_GLITCHING;
            fireGlitch();
        }
        return;
    }

    if (state == STATE_CALIBRATE) {
        // Passive timing measurement: count edges, measure period,
        // but do NOT fire any glitch.
        if (edgeCount == 0) {
            edgeCount = 1;
            firstEdgeCycles = now;
            lastEdgeCycles = now;
            return;
        }

        // Check for idle gap (new command, reset count)
        if ((now - lastEdgeCycles) > IDLE_TIMEOUT_CYCLES) {
            edgeCount = 1;
            firstEdgeCycles = now;
            lastEdgeCycles = now;
            return;
        }

        edgeCount++;
        lastEdgeCycles = now;

        // After enough edges, compute timing and signal main loop
        if (edgeCount >= edgeTarget) {
            if (edgeTarget > 1)
                measuredBitCycles = (now - firstEdgeCycles) / (edgeTarget - 1);
            timingReady = true;
            edgeCount = 0;  // ready for another measurement
        }
        return;
    }

    // In COOLDOWN, IDLE, or other states, just track timing.
    // In periodic (P) mode the state stays IDLE; a RISING edge here means
    // the target LED blinked off — a glitch hit the sensitive window.
    lastEdgeCycles = now;
    if (periodicPeriodUs > 0) hitEdgeDetected = true;
}

// Fire the glitch with cycle-accurate timing.
// Called from ISR context — busy-waits are intentional for precision.
// Maximum duration: ~83us delay + ~10us width = ~93us with interrupts off.
static void fireGlitch() {
    uint32_t delayCycles = nsToCycles(glitchDelayNs);
    uint32_t widthCycles = nsToCycles(glitchWidthNs);

    // Busy-wait for the configured delay from the 24th falling edge
    uint32_t target = edge24Cycles + delayCycles;
    while ((int32_t)(ARM_DWT_CYCCNT - target) < 0) {
        // spin — int32_t cast handles CYCCNT wrap correctly
    }

    // Switch to glitch VCC (74HC4053 selects Y1/X1/Z1 when MUX_CONTROL_PIN=HIGH)
    v_glitch();

    // Busy-wait for pulse width
    target = ARM_DWT_CYCCNT + widthCycles;
    while ((int32_t)(ARM_DWT_CYCCNT - target) < 0) {
        // spin
    }

    // Return to normal VCC (74HC4053 selects Y0/X0/Z0 when MUX_CONTROL_PIN=LOW)
    v_normal();

    glitchesTotal++;
    glitchFired = true;  // signal main loop
}

// Non-blocking serial command handler
static void handleSerial() {
    if (!Serial.available()) return;

    char cmd = Serial.read();
    if (cmd == '\r' || cmd == '\n') return;

    switch (cmd) {
        case 'D': case 'd': {
            long val = Serial.parseInt();
            if (val >= 0 && val <= 15000000) {
                glitchDelayNs = (uint32_t)val;
                Serial.printf("Delay: %lu ns (%lu cycles)\n",
                              glitchDelayNs, nsToCycles(glitchDelayNs));
            } else {
                Serial.printf("ERR: delay out of range (0-15000000)\n");
            }
            break;
        }
        case 'W': case 'w': {
            long val = Serial.parseInt();
            if (val >= 0 && val <= 50000) {
                glitchWidthNs = (uint32_t)val;
                Serial.printf("Width: %lu ns (%lu cycles)\n",
                              glitchWidthNs, nsToCycles(glitchWidthNs));
            } else {
                Serial.printf("ERR: width out of range (0-50000)\n");
            }
            break;
        }
        case 'R': case 'r':
            state = STATE_IDLE;
            continuous = false;
            resetModeActive = true;
            detachInterrupt(digitalPinToInterrupt(RESET_PIN));
            attachInterrupt(digitalPinToInterrupt(RESET_PIN), resetRisingISR, RISING);
            state = STATE_RESET_ARMED;
            Serial.println("ARMED (reset mode)");
            break;

        case 'A': case 'a':
            continuous = false;
            edgeCount = 0;
            lastEdgeCycles = ARM_DWT_CYCCNT;  // reset gap timer; prevents instant false HIT
            state = STATE_ARMED;
            Serial.println("ARMED (single-shot)");
            break;

        case 'C': case 'c':
            continuous = true;
            edgeCount = 0;
            lastEdgeCycles = ARM_DWT_CYCCNT;  // reset gap timer; prevents instant false HIT
            state = STATE_ARMED;
            Serial.println("ARMED (continuous)");
            break;

        case 'E': case 'e': {
            long val = Serial.parseInt();
            if (val >= 1 && val <= 255) {
                edgeTarget = (uint32_t)val;
                // E1 = LED-sync mode: target LED is active-low, so the sync pulse
                // is a RISING edge (LED pin goes HIGH when window starts).
                // E>1 = BDM mode: BDM bits are driven LOW → trigger on FALLING.
                detachInterrupt(digitalPinToInterrupt(BKGD_PIN));
                if (edgeTarget == 1) {
                    attachInterrupt(digitalPinToInterrupt(BKGD_PIN), bkgdISR, RISING);
                    Serial.printf("EdgeTarget: 1 (LED-sync, RISING edge)\n");
                } else {
                    attachInterrupt(digitalPinToInterrupt(BKGD_PIN), bkgdISR, FALLING);
                    Serial.printf("EdgeTarget: %lu (BDM mode, FALLING edge)\n", edgeTarget);
                }
            } else {
                Serial.printf("ERR: edge target out of range (1-255)\n");
            }
            break;
        }

        case 'Q': case 'q': {
            long val = Serial.parseInt();
            if (val < 0) {
                Serial.printf("ERR: sweep step must be >= 0\n");
                break;
            }
            sweepStepNs = (uint32_t)val;
            if (sweepStepNs > 0) {
                Serial.printf("SWEEP: step=%lu ns, max=%lu ns\n", sweepStepNs, sweepMaxNs);
            } else {
                Serial.println("SWEEP: disabled");
            }
            break;
        }

        case 'P': case 'p': {
            long val = Serial.parseInt();
            if (val < 0 || val > 1000000) {
                Serial.printf("ERR: period out of range (0-1000000 us)\n");
                break;
            }
            // Stop any running timer first
            periodicTimer.end();
            periodicPeriodUs = 0;
            if (val > 0) {
                periodicPeriodUs = (uint32_t)val;
                periodicCount = 0;
                periodicHits  = 0;
                periodicTimer.begin(periodicISR, (float)val);
                Serial.printf("PERIODIC: every %lu us, width %lu ns\n",
                              periodicPeriodUs, glitchWidthNs);
            } else {
                Serial.println("PERIODIC: stopped");
            }
            break;
        }

        case 'X': case 'x':
            state = STATE_IDLE;
            continuous = false;
            resetModeActive = false;
            periodicTimer.end();     // stop free-run timer if active
            periodicPeriodUs = 0;
            // Restore RESET_PIN to FALLING monitor mode (in case R-mode was active)
            detachInterrupt(digitalPinToInterrupt(RESET_PIN));
            attachInterrupt(digitalPinToInterrupt(RESET_PIN), resetISR, FALLING);
            v_normal();   // safety: ensure normal VCC selected (keep mux enabled)
            Serial.println("DISARMED");
            break;

        case 'S': case 's':
            printStatus();
            break;

        case 'T': case 't':
            // Calibration mode: passively observe edges, measure timing,
            // no glitch fired. Send any BDM command (e.g. USBDM_Connect
            // or USBDM_ReadDReg) while in this mode.
            state = STATE_CALIBRATE;
            edgeCount = 0;
            timingReady = false;
            Serial.println("CALIBRATE: waiting for BDM edges (send any BDM command)...");
            break;

        default:
            break;
    }
}

static void printStatus() {
    static const char *stateNames[] = {
        "IDLE", "ARMED", "COUNTING", "GLITCHING", "COOLDOWN", "CALIBRATE", "RESET_ARMED"
    };
    uint32_t now = ARM_DWT_CYCCNT;
    uint32_t sinceLastEdge = now - lastEdgeCycles;

    Serial.printf("--- Status ---\n");
    Serial.printf("State:      %s\n", stateNames[state]);
    Serial.printf("Continuous: %s\n", continuous ? "yes" : "no");
    Serial.printf("Delay:      %lu ns (%lu cycles)\n",
                  glitchDelayNs, nsToCycles(glitchDelayNs));
    Serial.printf("Width:      %lu ns (%lu cycles)\n",
                  glitchWidthNs, nsToCycles(glitchWidthNs));
    Serial.printf("EdgeTarget: %lu\n", edgeTarget);
    Serial.printf("Edges:      %lu / %lu\n", (uint32_t)edgeCount, edgeTarget);
    Serial.printf("Last edge:  %lu us ago\n", sinceLastEdge / 600);
    Serial.printf("Total glitches: %lu\n", (uint32_t)glitchesTotal);
    if (sweepStepNs > 0) {
        Serial.printf("Auto-sweep: step=%lu ns, max=%lu ns\n", sweepStepNs, sweepMaxNs);
    }
    if (periodicPeriodUs > 0) {
        Serial.printf("Periodic mode: every %lu us (%lu fired, %lu hits)\n",
                      periodicPeriodUs, periodicCount, periodicHits);
    }
    Serial.printf("RESET pin:  %s\n", digitalRead(RESET_PIN) ? "HIGH" : "LOW");

    if (measuredBitCycles > 0) {
        Serial.printf("--- Measured Timing ---\n");
        Serial.printf("Bit period:     %lu ns (raw cycles: %lu)\n",
                      bitPeriodNs(), (uint32_t)measuredBitCycles);
        Serial.printf("Bus clock:      %lu kHz\n", busClockKhz());
        Serial.printf("150-clk window: %lu ns\n", securityWindowNs());
    } else {
        Serial.printf("Timing: not yet measured (use T command or fire a glitch)\n");
    }
}
