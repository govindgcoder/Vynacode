# SPDX-License-Identifier: AGPL-3.0-only OR Commercial
import typer

app = typer.Typer()


@app.command()
def setup():
    print("Setup! to be implemented")
    pass


@app.command()
def index(path):
    print(f"Indexing path {path}. to be implemented")
    pass


@app.command()
def run(
    message: str,
    mode_name: str = "single",
    keep_loaded=False,
    context_window=None,
    no_approval=False,
    max_heal=4,
):
    print("Running! to be implemented")
    pass


@app.command()
def show(option: str = "plan"):
    if option == "plan":
        print("Show plan to be implemented")
    elif option == "context":
        print("Show context to be implemented")
    elif option == "index":
        print("Show index to be implemented")
    else:
        print(f"Invalid option {option}")
    pass


@app.command()
def revert():
    print("Revert! to be implemented")
    pass


@app.command()
def log():
    print("Log! to be implemented")
    pass


@app.command()
def models():
    print("Active models: ..... [to be implemented]")
    pass


@app.command()
def doctor():
    print("Doctor! to be implemented")
    pass


@app.command()
def activate(key):
    print(f"Activating {key} to be implemented")
    pass


@app.command()
def tier():
    print("Tier: .... [to be implemented]")
    pass


if __name__ == "__main__":
    app()
