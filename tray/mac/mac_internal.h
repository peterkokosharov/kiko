/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * mac_internal.h - shared declarations of the macOS layer.  UNVERIFIED: written without a Mac.
 */
#ifndef MAC_INTERNAL_H
#define MAC_INTERNAL_H

#include "kt_presenter.h"
#include "kt_view.h"

#ifdef __cplusplus
extern "C" {
#endif

int  mac_view_create(kt_view *out);   /* 0 = ok; call after NSApplication exists */
void mac_view_destroy(void);
/* launchctl control of the user LaunchAgent com.kiko.lite (blocking; worker thread). */
int  mac_host_service(void *ctx, kt_action action, char *msg, size_t msgsz);

#ifdef __cplusplus
}
#endif

#endif
