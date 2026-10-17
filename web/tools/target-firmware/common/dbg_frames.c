/* Deterministic frame/local-variable targets shared by F103 and H743.
 * Checkpoint symbols denote exact instructions, not compiler-dependent line stops.
 * Stack objects are deliberately volatile; register cases remain non-volatile.
 */
#include <stdint.h>
#define FRAME_FN __attribute__((noinline, noclone, optimize("no-optimize-sibling-calls")))
#define CHECKPOINT(name) __asm__ volatile(".global " #name "\n" #name ":\n nop" ::: "memory")
struct FrameRecord { uint32_t tag; int32_t signed_value; uint16_t pair[2]; };
volatile uint32_t g_frame_result;

FRAME_FN static uint32_t engine_frame_leaf(uint32_t arg)
{
    volatile uint32_t leaf_value = arg + 100u;
    volatile uint32_t items[3] = {arg, arg + 1u, arg + 2u};
    volatile struct FrameRecord record = {0x12345678u, -17, {21, 34}};
    CHECKPOINT(dbg_frame_leaf_checkpoint);
    return leaf_value + items[2] + record.pair[1] + arg;
}

FRAME_FN static uint32_t engine_frame_recursive(uint32_t depth, uint32_t seed)
{
    volatile uint32_t depth_copy = depth;
    volatile uint32_t seed_copy = seed;
    volatile uint32_t frame_value = seed + depth * 100u;
    uint32_t result = 0u;
    if (depth == 0u) {
        CHECKPOINT(dbg_frame_recursive_checkpoint);
        result = engine_frame_leaf(seed);
    } else {
        result = engine_frame_recursive(depth - 1u, seed + 7u);
    }
    return result + frame_value + seed_copy + depth_copy + depth + seed;
}

FRAME_FN static uint32_t engine_frame_shadow(uint32_t seed)
{
    volatile uint32_t shadow = seed + 10u;
    volatile uint32_t outer_copy = shadow;
    {
        volatile uint32_t shadow = seed + 20u;
        volatile uint32_t inner_copy = shadow;
        CHECKPOINT(dbg_frame_shadow_checkpoint);
        outer_copy += inner_copy;
    }
    CHECKPOINT(dbg_frame_shadow_exit_checkpoint);
    return shadow + outer_copy + seed;
}

FRAME_FN static uint32_t engine_frame_register(uint32_t arg)
{
    uint32_t register_value = arg * 3u + 1u;
    __asm__ volatile(".global dbg_frame_register_checkpoint\n"
                     "dbg_frame_register_checkpoint:\n nop"
                     : "+r"(arg), "+r"(register_value) :: "memory");
    return register_value ^ arg;
}

FRAME_FN static uint32_t engine_frame_migrate(uint32_t arg)
{
    volatile uint32_t stack_value = arg + 9u;
    __asm__ volatile(".global dbg_frame_before_call_checkpoint\n"
                     "dbg_frame_before_call_checkpoint:\n nop"
                     : "+r"(arg) :: "memory");
    uint32_t result = engine_frame_leaf(arg);
    __asm__ volatile(".global dbg_frame_after_call_checkpoint\n"
                     "dbg_frame_after_call_checkpoint:\n nop"
                     : "+m"(arg) :: "memory");
    return result + stack_value + arg;
}

FRAME_FN uint32_t engine_frame_stage(void)
{
    volatile uint32_t stage_cookie = 0x13579bdu;
    volatile uint32_t result = 0u;
    volatile uint32_t register_arg = 23u;
    result = engine_frame_recursive(4u, 1000u);
    result ^= engine_frame_shadow(50u);
    /* Keep the register-location check dynamic under -Os; a literal here may
     * become DW_OP_entry_value instead of a live register location. */
    result ^= engine_frame_register(register_arg);
    result ^= engine_frame_migrate(70u);
    g_frame_result = result + stage_cookie;
    return g_frame_result;
}
