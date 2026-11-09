/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_icon.h - renders the state icon (coloured disc with a white K) into a pixel buffer, so no
 * .ico/.png resource files are needed.  Shared by the Win32 and GTK layers.
 */
#ifndef KT_ICON_H
#define KT_ICON_H

#include <stdint.h>

#include "kt_view.h"

/* Fills size*size*4 bytes with straight (non-premultiplied) alpha pixels, top-down rows.
 * channel order: byte 0..3 = B,G,R,A when bgra != 0, else R,G,B,A.  size must be 8..256. */
void kt_icon_render(int size, kt_state state, int bgra, uint8_t *pixels);

/* The disc colour of a state as 0xRRGGBB. */
uint32_t kt_icon_state_rgb(kt_state state);

#endif
