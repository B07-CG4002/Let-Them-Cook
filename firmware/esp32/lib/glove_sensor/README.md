# glove_sensor (copy; do not edit)

Byte-identical copy of the glove owner's (Siqiang's) sensor module from
`hardware/gloves/integration_files/` (`glove_sensor.cpp`, `glove_sensor.h`; the
weights header is copied to `firmware/esp32/movement_model_params.h`, where the
module's `#include "../../../movement_model_params.h"` finds it, and beside the
`.cpp` in `src/`, so a plain `#include "movement_model_params.h"` works too).

Refresh with `python -m tools.sync_glove_module` after the module changes in the
team repo; `tests/test_glove_module_sync.py` fails while a copy differs. Only the
`firebeetle32-left-glove` and `firebeetle32-right-glove` environments link it.
