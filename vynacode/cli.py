# SPDX-License-Identifier: AGPL-3.0-only OR Commercial
import typer

app = typer.Typer()


@app.command()
def setup():
    print("Setup! to be implemented")
    pass


@app.command()
def index(path):
    # for files in walk(path)
    #   create filemetadata object
    #   call parse python file if .py or skip with only start and end bytes
    #   add the blocks to the filemetadata
    #   upsert it to the database
    #   call summarizer for the blocks
    # write the sql query results to codebase.json and only the one liner summaries start and end bytes to the codebase.md file
    # query: SELECT files.path, blocks.name, blocks.summary FROM files JOIN blocks ON files.path = blocks.parent_file
    # 
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
