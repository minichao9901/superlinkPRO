// SPDX-License-Identifier: GPL-2.0-or-later
#include <stdlib.h>

#include <emscripten.h>
#include <helper/command.h>
#include <server/server.h>

EM_ASYNC_JS(char *, next_browser_command, (), {
    const command = await Module.nextCommand();
    return command === null ? 0 : Module.stringToNewUTF8(command);
});

EM_JS(void, reply_to_browser, (int status, const char *result), {
    Module.commandResult(status, Module.UTF8ToString(result));
});

void __wrap_server_loop(struct command_context *ctx)
{
    for (;;) {
        char *command = next_browser_command();
        if (!command)
            return;

        int status = command_run_line(ctx, command);
        free(command);
        reply_to_browser(status, Jim_GetString(Jim_GetResult(ctx->interp), NULL));
    }
}
