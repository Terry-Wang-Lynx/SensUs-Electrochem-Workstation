#ifndef CONTROL_TRANSPORT_H_
#define CONTROL_TRANSPORT_H_

/*
 * Initialize the board's command/data transport before emitting CFG_BOOT.
 * V4.0 uses RTT and needs no explicit initialization.  V5.1 enables USB and
 * waits until the host opens the DATA CDC interface so startup audit lines
 * cannot be silently lost.
 */
int control_transport_init(void);

/* Non-blocking single-character receive: 1=character, 0=none, <0=error. */
int control_transport_read_char(char *ch);

#endif /* CONTROL_TRANSPORT_H_ */
