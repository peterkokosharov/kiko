/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * gtk_internal.h - shared declarations of the GTK3 layer (one tray instance per process).
 */
#ifndef GTK_INTERNAL_H
#define GTK_INTERNAL_H

#include <gtk/gtk.h>

#include "kt_presenter.h"
#include "kt_view.h"

/* gtk_view.c: fills *out; 0 = ok.  Must be called after gtk_init. */
int  gtk_view_create(kt_view *out);
void gtk_view_destroy(void);

/* gtk_host.c: `systemctl --user start|stop|restart kiko-lite` (blocking; worker thread). */
int  gtk_host_service(void *ctx, kt_action action, char *msg, size_t msgsz);

#endif
