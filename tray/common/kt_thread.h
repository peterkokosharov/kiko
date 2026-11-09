/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_thread.h - thin portable wrapper: Win32 threads/CRITICAL_SECTION/CONDITION_VARIABLE or pthreads,
 * plus a single-thread job worker.  Used so network work never runs on the UI thread.
 */
#ifndef KT_THREAD_H
#define KT_THREAD_H

#include <stdint.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <windows.h>
typedef CRITICAL_SECTION   kt_mutex;
typedef CONDITION_VARIABLE kt_cond;
typedef HANDLE             kt_thread;
#else
#include <pthread.h>
typedef pthread_mutex_t kt_mutex;
typedef pthread_cond_t  kt_cond;
typedef pthread_t       kt_thread;
#endif

typedef void (*kt_thread_fn)(void *arg);

void kt_mutex_init(kt_mutex *m);
void kt_mutex_destroy(kt_mutex *m);
void kt_mutex_lock(kt_mutex *m);
void kt_mutex_unlock(kt_mutex *m);

void kt_cond_init(kt_cond *c);
void kt_cond_destroy(kt_cond *c);
void kt_cond_signal(kt_cond *c);
void kt_cond_broadcast(kt_cond *c);
/* The mutex must be held.  Returns 0 when signalled (or spurious wakeup), 1 on timeout. */
int  kt_cond_wait_ms(kt_cond *c, kt_mutex *m, unsigned ms);

/* Returns 0 on success. */
int  kt_thread_start(kt_thread *t, kt_thread_fn fn, void *arg);
void kt_thread_join(kt_thread *t);

/* Monotonic milliseconds (arbitrary origin). */
uint64_t kt_now_ms(void);

/* ---- single-thread job worker ---- */
typedef struct kt_worker kt_worker;

kt_worker *kt_worker_new(void);
/* run(arg) executes on the worker thread.  drop(arg) (may be NULL) frees arg if the job is
 * discarded unrun by kt_worker_free. */
int        kt_worker_submit(kt_worker *w, kt_thread_fn run, kt_thread_fn drop, void *arg);
/* Waits until the queue is empty and no job is running; 0 = idle, 1 = timeout. */
int        kt_worker_wait_idle(kt_worker *w, unsigned timeout_ms);
/* Lets the running job finish, discards queued jobs, joins the thread. */
void       kt_worker_free(kt_worker *w);

#endif
