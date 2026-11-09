/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * qt_internal.h - C++17 Qt layer: includes the C core with extern "C" (UNVERIFIED: never compiled).
 */
#ifndef QT_INTERNAL_H
#define QT_INTERNAL_H

extern "C" {
#include "kt_icon.h"
#include "kt_presenter.h"
#include "kt_util.h"
#include "kt_view.h"
}

/* qt_view.cpp; needs a QApplication.  0 = ok. */
int  qt_view_create(kt_view *out);
void qt_view_destroy();

/* qt_host.cpp: `systemctl --user start|stop|restart kiko-lite` (blocking; worker thread). */
extern "C" int qt_host_service(void *ctx, kt_action action, char *msg, size_t msgsz);

#endif
