/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include <stdlib.h>
#include <string.h>

#include "kt_util.h"
#include "kt_view.h"

static char *dup_or_empty(const char *s) { return kt_strdup(s ? s : ""); }

kt_dialog *kt_dialog_new(const char *kind, const char *title)
{
    kt_dialog *d = (kt_dialog *)calloc(1, sizeof *d);
    if (!d) return NULL;
    d->kind = dup_or_empty(kind);
    d->title = dup_or_empty(title);
    d->summary = dup_or_empty("");
    d->text = dup_or_empty("");
    d->footer = dup_or_empty("");
    if (!d->kind || !d->title || !d->summary || !d->text || !d->footer) {
        kt_dialog_free(d);
        return NULL;
    }
    return d;
}

void kt_dialog_free(kt_dialog *d)
{
    int i;
    if (!d) return;
    free(d->kind);
    free(d->title);
    free(d->summary);
    free(d->text);
    free(d->footer);
    for (i = 0; i < d->ncols; i++) free(d->columns[i]);
    free(d->columns);
    for (i = 0; i < d->nrows * d->ncols; i++) free(d->cells[i]);
    free(d->cells);
    for (i = 0; i < d->nbuttons; i++) {
        free(d->buttons[i].id);
        free(d->buttons[i].label);
    }
    free(d->buttons);
    free(d);
}

static void set_str(char **slot, const char *s)
{
    char *n = dup_or_empty(s);
    if (!n) return;
    free(*slot);
    *slot = n;
}

void kt_dialog_set_summary(kt_dialog *d, const char *s) { if (d) set_str(&d->summary, s); }
void kt_dialog_set_text(kt_dialog *d, const char *s) { if (d) set_str(&d->text, s); }
void kt_dialog_set_footer(kt_dialog *d, const char *s) { if (d) set_str(&d->footer, s); }

int kt_dialog_add_column(kt_dialog *d, const char *name)
{
    char **nc;
    char *copy;
    if (!d || d->nrows > 0) return -1;
    nc = (char **)realloc(d->columns, (size_t)(d->ncols + 1) * sizeof *nc);
    if (!nc) return -1;
    d->columns = nc;
    copy = dup_or_empty(name);
    if (!copy) return -1;
    d->columns[d->ncols++] = copy;
    return 0;
}

int kt_dialog_add_row(kt_dialog *d, const char *const *cells)
{
    int i;
    char **nc;
    if (!d || d->ncols == 0) return -1;
    if (d->nrows == d->rowcap) {
        int ncap = d->rowcap ? d->rowcap * 2 : 16;
        nc = (char **)realloc(d->cells, (size_t)ncap * (size_t)d->ncols * sizeof *nc);
        if (!nc) return -1;
        d->cells = nc;
        d->rowcap = ncap;
    }
    for (i = 0; i < d->ncols; i++) {
        char *copy = dup_or_empty(cells ? cells[i] : NULL);
        if (!copy) {
            while (i-- > 0) free(d->cells[d->nrows * d->ncols + i]);
            return -1;
        }
        d->cells[d->nrows * d->ncols + i] = copy;
    }
    d->nrows++;
    return 0;
}

int kt_dialog_add_button(kt_dialog *d, const char *id, const char *label)
{
    kt_button *nb;
    char *i2, *l2;
    if (!d) return -1;
    if (d->nbuttons == d->btncap) {
        int ncap = d->btncap ? d->btncap * 2 : 4;
        nb = (kt_button *)realloc(d->buttons, (size_t)ncap * sizeof *nb);
        if (!nb) return -1;
        d->buttons = nb;
        d->btncap = ncap;
    }
    i2 = dup_or_empty(id);
    l2 = dup_or_empty(label);
    if (!i2 || !l2) { free(i2); free(l2); return -1; }
    d->buttons[d->nbuttons].id = i2;
    d->buttons[d->nbuttons].label = l2;
    d->nbuttons++;
    return 0;
}
