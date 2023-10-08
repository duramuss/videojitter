import asyncio
import importlib
import pathlib
import pkgutil
import subprocess
import sys


def _reset_directory(path):
    path.mkdir(exist_ok=True)
    for child in path.iterdir():
        child.unlink()


class _TestCase:
    def __init__(self, name):
        self.name = name
        self.module = importlib.import_module(f"videojitter.tests.{name}")
        self.output_dir = pathlib.Path("videojitter") / "tests" / name / "test_output"

    async def run(self):
        _reset_directory(self.output_dir)
        await self.module.videojitter_test(self)

    async def run_subprocess(self, name, *args):
        with open(f"{self.output_dir / name }.stdout", "wb") as stdout, open(
            f"{self.output_dir / name }.stderr", "wb"
        ) as stderr:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
            await process.communicate()
            if process.returncode != 0:
                raise Exception(
                    f"Subprocess {name} terminated with error code {process.returncode}"
                )


def main():
    tests_directory = pathlib.Path("videojitter") / "tests"
    for test_module_info in pkgutil.iter_modules([tests_directory]):
        # TODO: parallelize
        asyncio.run(_TestCase(test_module_info.name).run())


if __name__ == "__main__":
    sys.exit(main())
