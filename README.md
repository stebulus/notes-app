How to create an empty notes database:

    python -c 'import notes; notes.NotesDB.create("db")'

How to then use the application with that notes database:

    python notes.py db

How to run the tests in the source code:

    python -m doctest -v notes.py

The application was manually tested on 2023-02-04 with
Python 2.7.17, Whoosh 2.7.4, and wxPython 3.0.2.0.
