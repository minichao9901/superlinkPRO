// SPDX-License-Identifier: GPL-2.0-or-later
#include <stdint.h>
#include <stdlib.h>

#include <emscripten.h>
#include <helper/command.h>
#include <jtag/jtag.h>
#include <server/server.h>
#include <target/target.h>

int64_t __wrap_timeval_ms(void)
{
    /* OpenOCD picks CLOCK_MONOTONIC_RAW when the headers define it.
     * Emscripten's WASI clock does not implement that clock ID. */
    return (int64_t)emscripten_get_now();
}

EM_ASYNC_JS(char *, next_browser_command, (), {
    const command = await Module.nextCommand();
    return command === null ? 0 : Module.stringToNewUTF8(command);
});

EM_JS(void, reply_to_browser, (int status, const char *result), {
    Module.commandResult(status, Module.UTF8ToString(result));
});

EM_JS(int, browser_arm_memory_image, (), {
    return Module.isArmMemoryImage?.() ? 1 : 0;
});

void __wrap_server_loop(struct command_context *ctx)
{
    for (;;) {
        char *command = next_browser_command();
        if (!command)
            return;

        struct target *target = browser_arm_memory_image() ? get_current_target_or_null(ctx) : NULL;
        bool mask_poll = target && target->state == TARGET_HALTED;
        /* The workbench already owns a halted target. Avoid injecting a
         * redundant PPB exchange into its explicit binary memory command. */
        bool saved_poll_mask = mask_poll ? jtag_poll_mask() : false;
        int status = command_run_line(ctx, command);
        if (mask_poll)
            jtag_poll_unmask(saved_poll_mask);
        free(command);
        reply_to_browser(status, Jim_GetString(Jim_GetResult(ctx->interp), NULL));
    }
}
