/* A stand-in librtlsdr, compiled by test_sdr.py.
 *
 * Includes the real header, so the compiler proves these match the real API, and
 * ctypes then crosses a real C ABI. Each call is appended to $FAKE_RTL_LOG. It
 * delivers a fixed byte pattern, which proves the buffer plumbing end to end.
 */
#include <rtl-sdr.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct rtlsdr_dev { int corr; };
static struct rtlsdr_dev the_device;

static void note(const char *what, long value)
{
	FILE *f = fopen(getenv("FAKE_RTL_LOG"), "a");
	if (f) { fprintf(f, "%s %ld\n", what, value); fclose(f); }
}

static int fail_open(void) { return getenv("FAKE_RTL_BUSY") != NULL; }

uint32_t rtlsdr_get_device_count(void) { return 2; }
const char *rtlsdr_get_device_name(uint32_t index) { return index ? "Second" : "First"; }

int rtlsdr_get_device_usb_strings(uint32_t index, char *m, char *p, char *s)
{
	strcpy(m, "Realtek");
	strcpy(p, "RTL2838UHIDIR");
	strcpy(s, index ? "radio" : "00000001");
	return 0;
}

int rtlsdr_open(rtlsdr_dev_t **dev, uint32_t index)
{
	note("open", index);
	if (fail_open()) return -6;
	the_device.corr = 0;
	*dev = &the_device;
	return 0;
}

int rtlsdr_close(rtlsdr_dev_t *dev) { note("close", dev == &the_device); return 0; }
int rtlsdr_set_sample_rate(rtlsdr_dev_t *dev, uint32_t rate) { note("rate", rate); return 0; }
int rtlsdr_set_center_freq(rtlsdr_dev_t *dev, uint32_t freq) { note("tune", freq); return 0; }

int rtlsdr_set_freq_correction(rtlsdr_dev_t *dev, int ppm)
{
	note("ppm", ppm);
	if (dev->corr == ppm) return -2;
	dev->corr = ppm;
	return 0;
}

enum rtlsdr_tuner rtlsdr_get_tuner_type(rtlsdr_dev_t *dev)
{
	const char *t = getenv("FAKE_RTL_TUNER");
	return t ? (enum rtlsdr_tuner)atoi(t) : RTLSDR_TUNER_R820T;
}

/* Each tuner's own ladder, as in librtlsdr 2.0.2. */
int rtlsdr_get_tuner_gains(rtlsdr_dev_t *dev, int *gains)
{
	static const int fc0012[] = { -99, -40, 71, 179, 192 };
	static const int r82xx[] = { 0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207,
				     229, 254, 280, 297, 328, 338, 364, 372, 386, 402, 421,
				     434, 439, 445, 480, 496 };
	int is_fc0012 = rtlsdr_get_tuner_type(dev) == RTLSDR_TUNER_FC0012;
	const int *ladder = is_fc0012 ? fc0012 : r82xx;
	int count = is_fc0012 ? 5 : 29;
	if (gains) memcpy(gains, ladder, count * sizeof(int));
	return count;
}

int rtlsdr_set_tuner_gain_mode(rtlsdr_dev_t *dev, int manual) { note("manual", manual); return 0; }
int rtlsdr_set_tuner_gain(rtlsdr_dev_t *dev, int gain) { note("gain", gain); return 0; }
int rtlsdr_reset_buffer(rtlsdr_dev_t *dev) { note("reset", 0); return 0; }

int rtlsdr_read_sync(rtlsdr_dev_t *dev, void *buf, int len, int *n_read)
{
	unsigned char *b = buf;
	int i;
	for (i = 0; i < len; i += 2) { b[i] = 255; b[i + 1] = 0; }
	*n_read = len;
	return getenv("FAKE_RTL_READ_FAIL") ? -1 : 0;
}
