/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 */
#include "kt_thread.h"

#include <stdlib.h>

#ifndef _WIN32
#include <errno.h>
#include <time.h>
#endif

#ifdef _WIN32

void kt_mutex_init(kt_mutex *m) { InitializeCriticalSection(m); }
void kt_mutex_destroy(kt_mutex *m) { DeleteCriticalSection(m); }
void kt_mutex_lock(kt_mutex *m) { EnterCriticalSection(m); }
void kt_mutex_unlock(kt_mutex *m) { LeaveCriticalSection(m); }
void kt_cond_init(kt_cond *c) { InitializeConditionVariable(c); }
void kt_cond_destroy(kt_cond *c) { (void)c; }
void kt_cond_signal(kt_cond *c) { WakeConditionVariable(c); }
void kt_cond_broadcast(kt_cond *c) { WakeAllConditionVariable(c); }

int kt_cond_wait_ms(kt_cond *c, kt_mutex *m, unsigned ms)
{
    if (SleepConditionVariableCS(c, m, ms)) return 0;
    return GetLastError() == ERROR_TIMEOUT ? 1 : 0;
}

typedef struct { kt_thread_fn fn; void *arg; } tramp_arg;

static DWORD WINAPI tramp(LPVOID p)
{
    tramp_arg a = *(tramp_arg *)p;
    free(p);
    a.fn(a.arg);
    return 0;
}

int kt_thread_start(kt_thread *t, kt_thread_fn fn, void *arg)
{
    tramp_arg *a = (tramp_arg *)malloc(sizeof *a);
    if (!a) return -1;
    a->fn = fn;
    a->arg = arg;
    *t = CreateThread(NULL, 0, tramp, a, 0, NULL);
    if (!*t) { free(a); return -1; }
    return 0;
}

void kt_thread_join(kt_thread *t)
{
    WaitForSingleObject(*t, INFINITE);
    CloseHandle(*t);
    *t = NULL;
}

uint64_t kt_now_ms(void) { return (uint64_t)GetTickCount64(); }

#else /* POSIX */

#ifdef __APPLE__
#define KT_COND_CLOCK CLOCK_REALTIME
#else
#define KT_COND_CLOCK CLOCK_MONOTONIC
#endif

void kt_mutex_init(kt_mutex *m) { pthread_mutex_init(m, NULL); }
void kt_mutex_destroy(kt_mutex *m) { pthread_mutex_destroy(m); }
void kt_mutex_lock(kt_mutex *m) { pthread_mutex_lock(m); }
void kt_mutex_unlock(kt_mutex *m) { pthread_mutex_unlock(m); }

void kt_cond_init(kt_cond *c)
{
#ifdef __APPLE__
    pthread_cond_init(c, NULL);   /* macOS has no pthread_condattr_setclock: timed waits use CLOCK_REALTIME */
#else
    pthread_condattr_t a;
    pthread_condattr_init(&a);
    pthread_condattr_setclock(&a, CLOCK_MONOTONIC);
    pthread_cond_init(c, &a);
    pthread_condattr_destroy(&a);
#endif
}

void kt_cond_destroy(kt_cond *c) { pthread_cond_destroy(c); }
void kt_cond_signal(kt_cond *c) { pthread_cond_signal(c); }
void kt_cond_broadcast(kt_cond *c) { pthread_cond_broadcast(c); }

int kt_cond_wait_ms(kt_cond *c, kt_mutex *m, unsigned ms)
{
    struct timespec ts;
    clock_gettime(KT_COND_CLOCK, &ts);
    ts.tv_sec += (time_t)(ms / 1000);
    ts.tv_nsec += (long)(ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
    return pthread_cond_timedwait(c, m, &ts) == ETIMEDOUT ? 1 : 0;
}

typedef struct { kt_thread_fn fn; void *arg; } tramp_arg;

static void *tramp(void *p)
{
    tramp_arg a = *(tramp_arg *)p;
    free(p);
    a.fn(a.arg);
    return NULL;
}

int kt_thread_start(kt_thread *t, kt_thread_fn fn, void *arg)
{
    tramp_arg *a = (tramp_arg *)malloc(sizeof *a);
    if (!a) return -1;
    a->fn = fn;
    a->arg = arg;
    if (pthread_create(t, NULL, tramp, a) != 0) { free(a); return -1; }
    return 0;
}

void kt_thread_join(kt_thread *t) { pthread_join(*t, NULL); }

uint64_t kt_now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)(ts.tv_nsec / 1000000L);
}

#endif

/* ---- job worker ---- */

typedef struct job {
    kt_thread_fn run;
    kt_thread_fn drop;
    void        *arg;
    struct job  *next;
} job;

struct kt_worker {
    kt_mutex  mu;
    kt_cond   wake;   /* new job or stop */
    kt_cond   idle;   /* queue drained */
    job      *head;
    job      *tail;
    int       running;   /* a job is executing */
    int       stop;
    kt_thread th;
};

static void worker_main(void *arg)
{
    kt_worker *w = (kt_worker *)arg;
    kt_mutex_lock(&w->mu);
    for (;;) {
        job *j;
        while (!w->head && !w->stop) kt_cond_wait_ms(&w->wake, &w->mu, 1000);
        if (w->stop) break;
        j = w->head;
        w->head = j->next;
        if (!w->head) w->tail = NULL;
        w->running = 1;
        kt_mutex_unlock(&w->mu);
        j->run(j->arg);
        free(j);
        kt_mutex_lock(&w->mu);
        w->running = 0;
        if (!w->head) kt_cond_broadcast(&w->idle);
    }
    kt_cond_broadcast(&w->idle);
    kt_mutex_unlock(&w->mu);
}

kt_worker *kt_worker_new(void)
{
    kt_worker *w = (kt_worker *)calloc(1, sizeof *w);
    if (!w) return NULL;
    kt_mutex_init(&w->mu);
    kt_cond_init(&w->wake);
    kt_cond_init(&w->idle);
    if (kt_thread_start(&w->th, worker_main, w) != 0) {
        kt_cond_destroy(&w->wake);
        kt_cond_destroy(&w->idle);
        kt_mutex_destroy(&w->mu);
        free(w);
        return NULL;
    }
    return w;
}

int kt_worker_submit(kt_worker *w, kt_thread_fn run, kt_thread_fn drop, void *arg)
{
    job *j;
    if (!w || !run) return -1;
    j = (job *)malloc(sizeof *j);
    if (!j) return -1;
    j->run = run;
    j->drop = drop;
    j->arg = arg;
    j->next = NULL;
    kt_mutex_lock(&w->mu);
    if (w->stop) { kt_mutex_unlock(&w->mu); free(j); return -1; }
    if (w->tail) w->tail->next = j; else w->head = j;
    w->tail = j;
    kt_cond_signal(&w->wake);
    kt_mutex_unlock(&w->mu);
    return 0;
}

int kt_worker_wait_idle(kt_worker *w, unsigned timeout_ms)
{
    uint64_t deadline = kt_now_ms() + timeout_ms;
    int rc = 0;
    kt_mutex_lock(&w->mu);
    while (w->head || w->running) {
        uint64_t now = kt_now_ms();
        if (now >= deadline) { rc = 1; break; }
        kt_cond_wait_ms(&w->idle, &w->mu, (unsigned)(deadline - now));
    }
    kt_mutex_unlock(&w->mu);
    return rc;
}

void kt_worker_free(kt_worker *w)
{
    job *j;
    if (!w) return;
    kt_mutex_lock(&w->mu);
    w->stop = 1;
    kt_cond_broadcast(&w->wake);
    kt_mutex_unlock(&w->mu);
    kt_thread_join(&w->th);
    j = w->head;
    while (j) {
        job *n = j->next;
        if (j->drop) j->drop(j->arg);
        free(j);
        j = n;
    }
    kt_cond_destroy(&w->wake);
    kt_cond_destroy(&w->idle);
    kt_mutex_destroy(&w->mu);
    free(w);
}
