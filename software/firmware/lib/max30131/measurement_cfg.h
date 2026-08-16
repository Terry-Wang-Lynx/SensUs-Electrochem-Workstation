#ifndef MEASUREMENT_CFG_H_
#define MEASUREMENT_CFG_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MEASUREMENT_CFG_REQ_MAX 32

typedef struct {
	bool cv;
	int32_t start_mv;
	int32_t target_mv;
	uint32_t quiet_ms;
	uint32_t duration_ms;
	bool adaptive;
	uint32_t it_sample_interval_ms;
	int32_t cv_low_mv;
	int32_t cv_high_mv;
	uint32_t cv_rate_mv_s;
	uint16_t cv_cycles;
	uint16_t cv_step_mv;
	uint8_t cv_eis_fsr;
	bool it_use_eis;
	uint8_t it_eis_fsr;
} measurement_cfg_t;

typedef enum {
	MEAS_REJ_NONE = 0,
	MEAS_REJ_FORMAT,
	MEAS_REJ_REQUEST,
	MEAS_REJ_METHOD,
	MEAS_REJ_POTENTIAL,
	MEAS_REJ_QUIET,
	MEAS_REJ_DURATION,
	MEAS_REJ_ADAPTIVE,
	MEAS_REJ_SAMPLE_INTERVAL,
	MEAS_REJ_CV_RANGE,
	MEAS_REJ_CV_RATE,
	MEAS_REJ_CV_CYCLES,
	MEAS_REJ_CV_STEP,
	MEAS_REJ_EIS_RANGE,
} measurement_reject_t;

typedef struct {
	measurement_cfg_t cfg;
	char req[MEASUREMENT_CFG_REQ_MAX + 1];
} measurement_cmd_t;

const char *measurement_reject_name(measurement_reject_t reason);

/*
 * Full-snapshot command used by the portable host:
 * MEAS method start target quiet duration adaptive sample_interval
 *      cv_low cv_high cv_rate cv_cycles cv_step cv_eis it_use_eis it_eis req
 */
bool measurement_cfg_parse(const char *line, measurement_cmd_t *out,
			   measurement_reject_t *reason);

size_t measurement_cfg_format(const char *kind, const measurement_cfg_t *cfg,
			      const char *req, char *buffer, size_t size);

#endif /* MEASUREMENT_CFG_H_ */
