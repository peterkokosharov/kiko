/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include "kt_icon.h"

#include <math.h>

uint32_t kt_icon_state_rgb(kt_state state)
{
    switch (state) {
    case KT_STATE_OK:       return 0x2EA043;   /* green  */
    case KT_STATE_DEGRADED: return 0xF08800;   /* orange */
    case KT_STATE_DOWN:     return 0xD62828;   /* red    */
    case KT_STATE_STARTING:
    default:                return 0x2478E6;   /* blue   */
    }
}

static double clamp01(double v) { return v < 0.0 ? 0.0 : (v > 1.0 ? 1.0 : v); }

/* distance from point (px,py) to the segment (ax,ay)-(bx,by) */
static double seg_dist(double px, double py, double ax, double ay, double bx, double by)
{
    double dx = bx - ax, dy = by - ay;
    double t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy);
    double cx, cy;
    t = clamp01(t);
    cx = ax + t * dx - px;
    cy = ay + t * dy - py;
    return sqrt(cx * cx + cy * cy);
}

void kt_icon_render(int size, kt_state state, int bgra, uint8_t *pixels)
{
    uint32_t rgb = kt_icon_state_rgb(state);
    double cr = (double)((rgb >> 16) & 0xFF), cg = (double)((rgb >> 8) & 0xFF), cb = (double)(rgb & 0xFF);
    int x, y;

    for (y = 0; y < size; y++) {
        for (x = 0; x < size; x++) {
            double px = ((double)x + 0.5) / (double)size;
            double py = ((double)y + 0.5) / (double)size;
            double ddx = px - 0.5, ddy = py - 0.5;
            double disc = clamp01((0.47 - sqrt(ddx * ddx + ddy * ddy)) * (double)size + 0.5);
            double d, k, r, g, b;
            uint8_t *o = pixels + ((size_t)y * (size_t)size + (size_t)x) * 4u;

            /* the letter K: a stem and two arms, drawn as thick segments */
            d = seg_dist(px, py, 0.36, 0.27, 0.36, 0.73);
            {
                double d2 = seg_dist(px, py, 0.36, 0.52, 0.66, 0.27);
                double d3 = seg_dist(px, py, 0.40, 0.49, 0.66, 0.73);
                if (d2 < d) d = d2;
                if (d3 < d) d = d3;
            }
            k = clamp01((0.065 - d) * (double)size + 0.5);

            r = cr + (255.0 - cr) * k;
            g = cg + (255.0 - cg) * k;
            b = cb + (255.0 - cb) * k;
            o[bgra ? 2 : 0] = (uint8_t)(r + 0.5);
            o[1] = (uint8_t)(g + 0.5);
            o[bgra ? 0 : 2] = (uint8_t)(b + 0.5);
            o[3] = (uint8_t)(disc * 255.0 + 0.5);
        }
    }
}
