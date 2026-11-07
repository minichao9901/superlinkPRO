/* Development probe: upstream Jim Tcl evaluates configuration. No target or
 * adapter driver is linked. The command boundary records configuration only. */
#include <stdio.h>
#include <string.h>
#include "jim.h"

#define PATH_CAP 512
#define SOURCE_DEPTH 16
#define TRACE_LIMIT 8192

static Jim_Interp *interp;
static Jim_Obj *events;
static const char *transport;
static char search_root[PATH_CAP];
static const char *source_stack[SOURCE_DEPTH];
static int source_depth;

static int fail(Jim_Interp *ctx, const char *message, const char *detail)
{
    Jim_SetResultFormatted(ctx, "%s: %s", message, detail);
    return JIM_ERR;
}

/* Only absolute paths below /cfg and canonical relative paths are accepted. */
static int relative_path(const char *path)
{
    while (*path) {
        size_t length = strcspn(path, "/");
        if (!length || (length == 1 && path[0] == '.') ||
            (length == 2 && !strncmp(path, "..", 2)) ||
            memchr(path, '\\', length) || memchr(path, ':', length))
            return 0;
        if (!path[length])
            return 1;
        path += length + 1;
    }
    return 0;
}

static int make_path(const char *name, char out[PATH_CAP])
{
    int absolute = !strncmp(name, "/cfg/", 5);
    if (!relative_path(absolute ? name + 5 : name))
        return 0;
    int written = absolute ? snprintf(out, PATH_CAP, "%s", name)
                           : snprintf(out, PATH_CAP, "%s/%s", search_root, name);
    return written > 0 && written < PATH_CAP;
}

static int record(Jim_Interp *ctx, int argc, Jim_Obj *const *argv)
{
    if (Jim_ListLength(ctx, events) >= TRACE_LIMIT)
        return fail(ctx, "configuration command limit", Jim_String(argv[0]));
    Jim_ListAppendElement(ctx, events, Jim_NewListObj(ctx, argv, argc));
    Jim_SetEmptyResult(ctx);
    return JIM_OK;
}

static int evaluate_file(Jim_Interp *ctx, const char *name)
{
    char path[PATH_CAP];
    if (!make_path(name, path))
        return fail(ctx, "CFG path outside workspace", name);
    if (source_depth == SOURCE_DEPTH)
        return fail(ctx, "CFG source depth limit", path);
    for (int i = 0; i < source_depth; ++i) {
        if (!strcmp(path, source_stack[i]))
            return fail(ctx, "CFG source cycle", path);
    }
    source_stack[source_depth++] = path;
    Jim_SetEmptyResult(ctx);
    int status = Jim_EvalFile(ctx, path);
    --source_depth;
    return status;
}

static int file_command(Jim_Interp *ctx, int argc, Jim_Obj *const *argv)
{
    if (argc != 2) {
        Jim_WrongNumArgs(ctx, 1, argv, "file");
        return JIM_ERR;
    }
    const char *name = Jim_String(argv[1]);
    if (!strcmp(Jim_String(argv[0]), "source")) {
        if (record(ctx, argc, argv) != JIM_OK)
            return JIM_ERR;
        return evaluate_file(ctx, name);
    }
    char path[PATH_CAP];
    if (!make_path(name, path))
        return fail(ctx, "CFG path outside workspace", name);
    FILE *file = fopen(path, "rb");
    if (!file)
        return fail(ctx, "CFG file missing", path);
    fclose(file);
    Jim_SetResultString(ctx, path, -1);
    return JIM_OK;
}

static int instance_command(Jim_Interp *ctx, int argc, Jim_Obj *const *argv)
{
    if (argc >= 2) {
        const char *sub = Jim_String(argv[1]);
        if (!strcmp(sub, "configure") || !strcmp(sub, "apcsw") ||
            (argc == 4 && !strcmp(sub, "cortex_m") &&
             !strcmp(Jim_String(argv[2]), "reset_config")))
            return record(ctx, argc, argv);
    }
    return fail(ctx, "unsupported target command", Jim_String(argv[0]));
}

static int directive(Jim_Interp *ctx, int argc, Jim_Obj *const *argv)
{
    const char *command = Jim_String(argv[0]);
    const char *sub = argc > 1 ? Jim_String(argv[1]) : "";
    if (!strcmp(command, "using_swd") || !strcmp(command, "using_jtag") ||
        !strcmp(command, "using_hla")) {
        Jim_SetResultBool(ctx, !strcmp(command + 6, transport));
        return JIM_OK;
    }
    if (!strcmp(command, "transport") && !strcmp(sub, "select")) {
        if (argc == 3) {
            const char *value = Jim_String(argv[2]);
            if (!strcmp(value, "swd")) transport = "swd";
            else if (!strcmp(value, "jtag")) transport = "jtag";
            else return fail(ctx, "unsupported transport", value);
            if (record(ctx, argc, argv) != JIM_OK)
                return JIM_ERR;
        } else if (argc != 2) {
            return fail(ctx, "wrong transport arguments", sub);
        }
        Jim_SetResultString(ctx, transport, -1);
        return JIM_OK;
    }
    if ((!strcmp(command, "target") || !strcmp(command, "dap") ||
         !strcmp(command, "swo") || !strcmp(command, "tpiu") ||
         !strcmp(command, "cti")) && !strcmp(sub, "create") && argc >= 3) {
        if (Jim_GetCommand(ctx, argv[2], JIM_NONE))
            return fail(ctx, "configuration command name already exists", Jim_String(argv[2]));
        Jim_CreateCommand(ctx, Jim_String(argv[2]), instance_command, NULL, NULL);
        return record(ctx, argc, argv);
    }
    if ((!strcmp(command, "flash") && !strcmp(sub, "bank")) ||
        (!strcmp(command, "adapter") && (!strcmp(sub, "driver") ||
          !strcmp(sub, "speed") || !strcmp(sub, "srst"))) ||
        (!strcmp(command, "swd") && !strcmp(sub, "newdap")) ||
        (!strcmp(command, "jtag") && !strcmp(sub, "newtap")) ||
        (!strcmp(command, "riscv") && !strcmp(sub, "expose_csrs")) ||
        (!strcmp(command, "cortex_m") && !strcmp(sub, "reset_config")) ||
        !strcmp(command, "reset_config") || !strcmp(command, "targets") ||
        !strcmp(command, "bindto") || !strcmp(command, "add_help_text") ||
        !strcmp(command, "add_usage_text") || !strcmp(command, "echo"))
        return record(ctx, argc, argv);
    return fail(ctx, "unsupported OpenOCD command in configuration capture", command);
}

int cfg_create(const char *mode, const char *root)
{
    if (interp) {
        Jim_DecrRefCount(interp, events);
        Jim_FreeInterp(interp);
        interp = NULL;
    }
    transport = !strcmp(mode, "jtag") ? "jtag" : "swd";
    if (*root && !relative_path(root))
        return 0;
    int size = snprintf(search_root, sizeof(search_root), "/cfg%s%s", *root ? "/" : "", root);
    if (size < 0 || (size_t)size >= sizeof(search_root))
        return 0;
    interp = Jim_CreateInterp();
    Jim_RegisterCoreCommands(interp);
    const char *disabled[] = {"env", "exit", "puts", "source", NULL};
    for (int i = 0; disabled[i]; ++i) {
        Jim_Obj *name = Jim_NewStringObj(interp, disabled[i], -1);
        Jim_IncrRefCount(name);
        Jim_DeleteCommand(interp, name);
        Jim_DecrRefCount(interp, name);
    }
    Jim_CreateCommand(interp, "source", file_command, NULL, NULL);
    Jim_CreateCommand(interp, "find", file_command, NULL, NULL);
    const char *commands[] = {
        "using_swd", "using_jtag", "using_hla", "transport", "target", "dap",
        "swo", "tpiu", "cti", "flash", "adapter", "swd", "jtag", "riscv",
        "cortex_m", "reset_config", "targets", "bindto", "add_help_text",
        "add_usage_text", "echo", NULL,
    };
    for (int i = 0; commands[i]; ++i)
        Jim_CreateCommand(interp, commands[i], directive, NULL, NULL);
    events = Jim_NewListObj(interp, NULL, 0);
    Jim_IncrRefCount(events);
    source_depth = 0;
    return 1;
}

int cfg_set(const char *name, const char *value)
{
    return Jim_SetVariableStrWithStr(interp, name, value);
}

int cfg_run(const char *entry)
{
    return evaluate_file(interp, entry);
}

const char *cfg_result(void)
{
    return Jim_String(Jim_GetResult(interp));
}

int cfg_count(void)
{
    return Jim_ListLength(interp, events);
}

int cfg_arity(int index)
{
    Jim_Obj *row;
    return Jim_ListIndex(interp, events, index, &row, JIM_NONE) == JIM_OK
        ? Jim_ListLength(interp, row) : 0;
}

const char *cfg_arg(int index, int arg)
{
    Jim_Obj *row, *value;
    if (Jim_ListIndex(interp, events, index, &row, JIM_NONE) != JIM_OK ||
        Jim_ListIndex(interp, row, arg, &value, JIM_NONE) != JIM_OK)
        return "";
    return Jim_String(value);
}
