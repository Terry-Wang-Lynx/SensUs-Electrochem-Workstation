# L-DOPA I-T: +0.2 V / 180 s

This directory archives the hardware and analysis protocol verified on
2026-08-12. It is independent of local GUI state and experimental data.

## Acquisition

- Technique: constant-potential I-T
- Potential: +0.2 V for the complete sampled interval
- Duration: 180 s
- Current FSR: 2 uA
- Offset: 10% FSR (nominal 200 nA)
- AFE period: 124 ms (native rate approximately 8.0645 Hz)
- Host output: 10 Hz, 1800 points
- Calibration/prediction value: mean of valid samples in the final 20 s

The 10 Hz series is a timestamp-based resampling of the native AFE samples;
it is not 1800 independent hardware conversions. Saturated or invalid samples
remain marked invalid and must not enter calibration or prediction.

## Effective AFE Baseline

The verified boot configuration is equivalent to:

```text
SET fsr=5 off=1 conv=auto period=0 e=200 vwe=1200 idle=2 sysper=3 cellv=1 ioc=0 rs=0
```

With this FSR and period, `conv=auto` resolves to CONV_TIME code 2 (14 bit).
`measurement_config.h` is the archived compile-time header, while
`protocol.json` is the machine-readable GUI and audit profile.

Changing duration, potential, FSR, offset, sampling period, output rate, or
fit window creates a different calibration protocol. Data from such protocols
must not be silently combined into one calibration curve.
