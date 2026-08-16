#include "measurement_cfg.h"

#include <stdio.h>
#include <string.h>

static bool request_char_ok(char value)
{
	return (value >= '0' && value <= '9') ||
	       (value >= 'A' && value <= 'Z') ||
	       (value >= 'a' && value <= 'z') || value == '-';
}

const char *measurement_reject_name(measurement_reject_t reason)
{
	switch (reason) {
	case MEAS_REJ_NONE: return "none";
	case MEAS_REJ_FORMAT: return "format";
	case MEAS_REJ_REQUEST: return "request";
	case MEAS_REJ_METHOD: return "method";
	case MEAS_REJ_POTENTIAL: return "potential";
	case MEAS_REJ_QUIET: return "quiet";
	case MEAS_REJ_DURATION: return "duration";
	case MEAS_REJ_ADAPTIVE: return "adaptive";
	case MEAS_REJ_SAMPLE_INTERVAL: return "sample_interval";
	case MEAS_REJ_CV_RANGE: return "cv_range";
	case MEAS_REJ_CV_RATE: return "cv_rate";
	case MEAS_REJ_CV_CYCLES: return "cv_cycles";
	case MEAS_REJ_CV_STEP: return "cv_step";
	case MEAS_REJ_EIS_RANGE: return "eis_range";
	default: return "unknown";
	}
}

static bool validate(const int values[15], measurement_reject_t *reason)
{
	const int method = values[0];
	const int start_mv = values[1];
	const int target_mv = values[2];
	const int quiet_ms = values[3];
	const int duration_ms = values[4];
	const int adaptive = values[5];
	const int sample_interval_ms = values[6];
	const int cv_low_mv = values[7];
	const int cv_high_mv = values[8];
	const int cv_rate_mv_s = values[9];
	const int cv_cycles = values[10];
	const int cv_step_mv = values[11];
	const int cv_eis_fsr = values[12];
	const int it_use_eis = values[13];
	const int it_eis_fsr = values[14];

	if (method < 0 || method > 1) {
		*reason = MEAS_REJ_METHOD;
		return false;
	}
	if (adaptive < 0 || adaptive > 1 || it_use_eis < 0 || it_use_eis > 1) {
		*reason = MEAS_REJ_ADAPTIVE;
		return false;
	}
	if (quiet_ms < 0 || quiet_ms > 300000) {
		*reason = MEAS_REJ_QUIET;
		return false;
	}
	if (sample_interval_ms < 100 || sample_interval_ms > 2000) {
		*reason = MEAS_REJ_SAMPLE_INTERVAL;
		return false;
	}
	if (cv_eis_fsr < 0 || cv_eis_fsr > 3 ||
	    it_eis_fsr < 0 || it_eis_fsr > 3) {
		*reason = MEAS_REJ_EIS_RANGE;
		return false;
	}
	if (method == 0) {
		if (start_mv < -400 || start_mv > 400 ||
		    target_mv < -400 || target_mv > 400) {
			*reason = MEAS_REJ_POTENTIAL;
			return false;
		}
		if (duration_ms <= 0 || duration_ms > 3600000 ||
		    (adaptive == 0 && duration_ms < 10000)) {
			*reason = MEAS_REJ_DURATION;
			return false;
		}
	} else {
		if (adaptive != 0) {
			*reason = MEAS_REJ_ADAPTIVE;
			return false;
		}
		if (cv_low_mv < -600 || cv_high_mv > 600 ||
		    cv_low_mv >= cv_high_mv || start_mv != cv_low_mv ||
		    target_mv != cv_low_mv) {
			*reason = MEAS_REJ_CV_RANGE;
			return false;
		}
		if (cv_rate_mv_s < 10 || cv_rate_mv_s > 100) {
			*reason = MEAS_REJ_CV_RATE;
			return false;
		}
		if (cv_cycles < 1 || cv_cycles > 100) {
			*reason = MEAS_REJ_CV_CYCLES;
			return false;
		}
		if (cv_step_mv != 1) {
			*reason = MEAS_REJ_CV_STEP;
			return false;
		}
		if (duration_ms <= 0 || duration_ms > 86400000) {
			*reason = MEAS_REJ_DURATION;
			return false;
		}
	}
	*reason = MEAS_REJ_NONE;
	return true;
}

bool measurement_cfg_parse(const char *line, measurement_cmd_t *out,
			   measurement_reject_t *reason)
{
	int values[15];
	char req[MEASUREMENT_CFG_REQ_MAX + 1];
	char trailing;
	int parsed;

	if (reason != NULL) {
		*reason = MEAS_REJ_FORMAT;
	}
	if (line == NULL || out == NULL || reason == NULL) {
		return false;
	}
	parsed = sscanf(
		line,
		"MEAS %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %32s %c",
		&values[0], &values[1], &values[2], &values[3], &values[4],
		&values[5], &values[6], &values[7], &values[8], &values[9],
		&values[10], &values[11], &values[12], &values[13], &values[14],
		req, &trailing);
	if (parsed != 16) {
		return false;
	}
	if (req[0] == '\0') {
		*reason = MEAS_REJ_REQUEST;
		return false;
	}
	for (size_t i = 0; req[i] != '\0'; i++) {
		if (!request_char_ok(req[i])) {
			*reason = MEAS_REJ_REQUEST;
			return false;
		}
	}
	if (!validate(values, reason)) {
		return false;
	}
	memset(out, 0, sizeof(*out));
	out->cfg.cv = values[0] != 0;
	out->cfg.start_mv = values[1];
	out->cfg.target_mv = values[2];
	out->cfg.quiet_ms = (uint32_t)values[3];
	out->cfg.duration_ms = (uint32_t)values[4];
	out->cfg.adaptive = values[5] != 0;
	out->cfg.it_sample_interval_ms = (uint32_t)values[6];
	out->cfg.cv_low_mv = values[7];
	out->cfg.cv_high_mv = values[8];
	out->cfg.cv_rate_mv_s = (uint32_t)values[9];
	out->cfg.cv_cycles = (uint16_t)values[10];
	out->cfg.cv_step_mv = (uint16_t)values[11];
	out->cfg.cv_eis_fsr = (uint8_t)values[12];
	out->cfg.it_use_eis = values[13] != 0;
	out->cfg.it_eis_fsr = (uint8_t)values[14];
	memcpy(out->req, req, strlen(req) + 1U);
	return true;
}

size_t measurement_cfg_format(const char *kind, const measurement_cfg_t *cfg,
			      const char *req, char *buffer, size_t size)
{
	int written;

	if (kind == NULL || cfg == NULL || req == NULL || buffer == NULL || size == 0U) {
		return 0U;
	}
	written = snprintf(
		buffer, size,
		"%s req=%s mode=%d start_mv=%d target_mv=%d quiet_ms=%u "
		"duration_ms=%u adaptive=%d sample_interval_ms=%u cv_low_mv=%d "
		"cv_high_mv=%d cv_rate_mv_s=%u cv_cycles=%u cv_step_mv=%u "
		"cv_eis=%u it_use_eis=%d it_eis=%u",
		kind, req, cfg->cv ? 1 : 0, cfg->start_mv, cfg->target_mv,
		cfg->quiet_ms, cfg->duration_ms, cfg->adaptive ? 1 : 0,
		cfg->it_sample_interval_ms, cfg->cv_low_mv, cfg->cv_high_mv,
		cfg->cv_rate_mv_s, cfg->cv_cycles, cfg->cv_step_mv,
		cfg->cv_eis_fsr, cfg->it_use_eis ? 1 : 0, cfg->it_eis_fsr);
	if (written < 0 || (size_t)written >= size) {
		return 0U;
	}
	return (size_t)written;
}
