/*
 * json.h - minimal JSON reader (RFC 8259) and JSON-lines writer used by
 * Endpoint B for test vectors, plan files and logs.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#ifndef PBS_JSON_H
#define PBS_JSON_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef enum { JSON_NULL, JSON_BOOL, JSON_NUMBER, JSON_STRING, JSON_ARRAY, JSON_OBJECT } json_type_t;

typedef struct json_value {
    json_type_t         type;
    int                 boolean;
    int                 is_integer;  /* number without fraction or exponent */
    int64_t             integer;     /* valid when is_integer */
    double              number;
    char               *string;      /* UTF-8, NUL-terminated */
    size_t              string_len;  /* may contain NUL bytes from \u0000 */
    size_t              count;       /* array items or object members */
    struct json_value **items;
    char              **keys;        /* object member names */
} json_value_t;

/* Parse one JSON text. On error returns NULL and writes a message. */
json_value_t *json_parse(const char *text, size_t len, char *err, size_t errcap);
void json_free(json_value_t *v);

/* Lookups return NULL when absent or of another type. */
const json_value_t *json_get(const json_value_t *obj, const char *key);
const json_value_t *json_at(const json_value_t *arr, size_t i);
const char *json_get_str(const json_value_t *obj, const char *key);
/* 0 on success (integer present), -1 otherwise. */
int json_get_int(const json_value_t *obj, const char *key, int64_t *out);
/* 0 on success (any number), -1 otherwise. */
int json_get_num(const json_value_t *obj, const char *key, double *out);
/* 1 true, 0 false, def if absent. */
int json_get_bool(const json_value_t *obj, const char *key, int def);

/* Structural equality; numbers compare as integers when both are. */
int json_equal(const json_value_t *a, const json_value_t *b);

/* Read a whole file; caller frees. NULL on error. */
char *json_read_file(const char *path, size_t *len);

/* ---- Writer: one object per line ------------------------------------------------ */

typedef struct {
    char  *buf;
    size_t len, cap;
    int    first;    /* no member written yet at the current level */
    int    oom;
} jw_t;

void jw_init(jw_t *w);
void jw_free(jw_t *w);
void jw_reset(jw_t *w);
void jw_obj_begin(jw_t *w);
void jw_obj_end(jw_t *w);
void jw_key(jw_t *w, const char *key);            /* then exactly one value */
void jw_arr_begin(jw_t *w);
void jw_arr_end(jw_t *w);
void jw_str(jw_t *w, const char *s);
void jw_strn(jw_t *w, const char *s, size_t n);   /* bytes escaped as JSON */
void jw_int(jw_t *w, int64_t v);
void jw_uint(jw_t *w, uint64_t v);
void jw_num(jw_t *w, double v);
void jw_bool(jw_t *w, int v);
void jw_null(jw_t *w);
void jw_raw(jw_t *w, const char *json);           /* a serialized JSON value */
/* Convenience: key + value. */
void jw_kstr(jw_t *w, const char *k, const char *s);
void jw_kint(jw_t *w, const char *k, int64_t v);
void jw_kuint(jw_t *w, const char *k, uint64_t v);
void jw_knum(jw_t *w, const char *k, double v);
void jw_kbool(jw_t *w, const char *k, int v);
void jw_knull(jw_t *w, const char *k);
void jw_khex(jw_t *w, const char *k, const uint8_t *b, size_t n);
const char *jw_cstr(jw_t *w);

#endif
