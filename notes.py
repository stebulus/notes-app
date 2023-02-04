#!/usr/bin/env python
from datetime import datetime
import errno
import os
import socket
import threading
import whoosh.analysis
import whoosh.index
import whoosh.fields
import whoosh.query
import wx
import wx.lib.newevent as newevent

def testingthread(*args, **kwargs):
    t = threading.Thread(*args, **kwargs)
    t.daemon = True
    t.start()
    return t

def failifclosed(func):
    """Function decorator: Assert not self.closed."""
    def assertnotclosed(self, *args, **kwargs):
        if self.closed:
            raise IOError('%r is closed' % (self,))
        return func(self, *args, **kwargs)
    return assertnotclosed

def ignoreifclosed(func):
    """Function decorator: If self.closed, then nop."""
    def checknotclosed(self, *args, **kwargs):
        if not self.closed:
            return func(self, *args, **kwargs)
    return checknotclosed

def callafter(func1, func2, *func2args, **func2kwargs):
    """Function wrapper: call func2 after func1 returns."""
    def aftercaller(*args, **kwargs):
        val = func1(*args, **kwargs)
        func2(*func2args, **func2kwargs)
        return val
    return aftercaller

def queueiter(q, block=True):
    """Return an iterator of the given queue, blocking by default."""
    try:
        while True:
            yield q.get(block=block)
    except EOFError:
        raise StopIteration

class NotesDB(object):
    def __init__(self, dirname, index):
        self._dirname = dirname
        self._index = index
        self._idmaker = IDMaker()
        self._searcher = index.searcher()

    ANALYZER = whoosh.analysis.StandardAnalyzer(stoplist=None, minsize=0)
    SCHEMA = whoosh.fields.Schema(
        id=whoosh.fields.ID(stored=True, unique=True),
        indextime=whoosh.fields.DATETIME(stored=True),
        title=whoosh.fields.STORED,
        content=whoosh.fields.TEXT(analyzer=ANALYZER),
        )

    @classmethod
    def _indexsubdir(cls, dirname):
        return os.path.join(dirname, "index")

    @classmethod
    def open(cls, dirname):
        return NotesDB(dirname, whoosh.index.open_dir(cls._indexsubdir(dirname)))

    @classmethod
    def create(cls, dirname, overwrite=False):
        if not os.path.exists(dirname):
            os.mkdir(dirname)
        indexdir = cls._indexsubdir(dirname)
        if not os.path.exists(indexdir):
            os.mkdir(indexdir)
        if not overwrite and whoosh.index.exists_in(indexdir):
            raise IOError(errno.EEXIST, os.strerror(errno.EEXISTS), indexdir)
        return NotesDB(dirname, whoosh.index.create_in(indexdir, cls.SCHEMA))

    def __str__(self):
        s = 'NotesDB at ' + self._dirname
        if self.closed:
            s += ' (closed)'
        return s

    def close(self):
        if self._index is not None:
            self._index.close()
            self._index = None
        if self._searcher is not None:
            self._searcher.close()
            self._searcher = None

    @property
    def closed(self):
        return self._index is None

    @property
    def dirname(self):
        if self.closed:
            return None
        else:
            return self._dirname

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False  # let exception propagate

    @failifclosed
    def id2filename(self, id):
        return os.path.join(self._dirname, id)

    @failifclosed
    def remove(self, id):
        os.remove(self.id2filename(id))
        with self._index.writer() as writer:
            writer.delete_by_term('id', unicode(id))
        self._searcher = self._index.searcher()

    @failifclosed
    def info(self, id):
        return self._searcher.document(id=unicode(id))

    @failifclosed
    def search(self, querystr):
        return self._searcher.search(self._query(querystr), limit=None)

    def _query(self, querystr):
        words = [t.text for t in self.ANALYZER(querystr)]
        if not words:
            raise ValueError("empty query")
        terms = [whoosh.query.Term("content", word) for word in words]
        if not querystr.endswith(' '):
            del terms[-1]
            terms.append(whoosh.query.Prefix("content", words[-1]))
        return whoosh.query.And(terms)

    @failifclosed
    def read(self, id):
        with open(self.id2filename(id), 'r') as fil:
            return fil.read().decode('utf8')

    @failifclosed
    def write(self, text, id=None):
        if type(text) is str:
            text = text.decode('utf-8')
        if id is None:
            id = self._idmaker.newid()
        filename = self.id2filename(id)
        with open(filename + '.new', 'w') as fil:
            fil.write(text.encode('utf8'))
        os.rename(filename + '.new', filename)
        with self._index.writer() as writer:
            writer.update_document(
                id=unicode(id),
                title=text.splitlines()[0],
                indextime=datetime.utcnow(),
                content=text,
                )
        self._searcher = self._index.searcher()
        return id

class IDMaker(object):  # note: not threadsafe
    def __init__(self):
        self._selfid = '%s_%s_%s' % (
            socket.gethostname(),
            os.getpid(),
            id(self),
            )
        self._lasttimestr = None
        self._extra = 0
    def newid(self):
        timestr = datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')
        if timestr == self._lasttimestr:
            self._extra += 1
        else:
            self._extra = 0
        self._lasttimestr = timestr
        return '%s_%s_%s' % (timestr, self._selfid, self._extra)

class PreQueue(object):
    """Base class for queue-like objects.  See MockQueue for how to subclass."""
    def __init__(self):
        self._condition = threading.Condition()
        self._closed = False

    def put(self, item):
        """Add an item to the queue."""
        with self._condition:
            if self._closed:
                raise ValueError("operation on closed queue")
            if self._store(item):
                self._condition.notifyAll()

    def get(self, block=True):
        """Get an item from the queue.

        By default, this call blocks until an item is added to the queue.

        >>> q = MockQueue()
        >>> item = None
        >>> def fetch():
        ...     global item
        ...     item = q.get()
        >>> t = testingthread(target=fetch)
        >>> t.join(0.1); t.isAlive()  # still blocked waiting for an item
        True
        >>> q.put('a')
        >>> t.join(0.1); t.isAlive()
        False
        >>> item
        'a'

        If block=False, throw EOFError when there is no item in the queue.

        >>> q = MockQueue()
        >>> q.put('a')
        >>> q.get(block=False)
        'a'
        >>> q.get(block=False)
        Traceback (most recent call last):
        ...
        EOFError

        If the queue is closed, blocking get() raises ValueError,
        but nonblocking get() will just raise EOFError (just like
        when the queue is open but empty).

        >>> q = MockQueue()
        >>> q.close()
        >>> q.get()
        Traceback (most recent call last):
        ...
        ValueError: operation on closed queue
        >>> q.get(block=False)
        Traceback (most recent call last):
        ...
        EOFError
        """
        with self._condition:
            if block:
                if self._closed:
                    raise ValueError("operation on closed queue")
                while not self._itemavailable():
                    self._condition.wait()
                    if self._closed:
                        raise EOFError
            else:
                if not self._itemavailable():
                    raise EOFError
            return self._yielditem()

    def close(self):
        """Close the queue.

        Adding items to a closed queue is an error.

        >>> q = MockQueue()
        >>> q.close()
        >>> q.put('a')
        Traceback (most recent call last):
        ...
        ValueError: operation on closed queue

        """
        with self._condition:
            self._closed = True
            self._condition.notifyAll()

    def join(self):
        """Block until the queue is empty and all items have been processed.

        >>> q = MockQueue()
        >>> q.put('a')
        >>> t = testingthread(target=q.join)
        >>> t.join(0.1); t.isAlive()  # waiting for 'a' to be extracted
        True
        >>> q.get()
        'a'
        >>> t.join(0.1); t.isAlive()  # waiting for 'a' to be processed
        True
        >>> q.task_done()  # implemented by subclass, or not
        >>> t.join(0.1); t.isAlive()
        False

        Joining a closed queue immediately returns.

        >>> q = MockQueue()
        >>> q.close()
        >>> t = testingthread(target=q.join)
        >>> t.join(0.1); t.isAlive()
        False

        Closing a queue causes joined threads to unblock immediately,
        whether or not there are outstanding items.

        >>> q = MockQueue()
        >>> q.put('a')
        >>> t = testingthread(target=q.join)
        >>> t.join(0.1); t.isAlive()
        True
        >>> q.close()
        >>> t.join(0.1); t.isAlive()
        False

        Closing a queue also causes blocked threads to raise EOFError.

        >>> q = MockQueue()
        >>> def eof():
        ...     try:
        ...         q.get()
        ...     except EOFError:
        ...         pass
        >>> t = testingthread(target=eof)
        >>> t.join(0.1); t.isAlive()
        True
        >>> q.close()
        >>> t.join(0.1); t.isAlive()
        False
        """
        with self._condition:
            while not self._closed \
                    and (self._itemavailable() or self._itemspending()):
                self._condition.wait()

class MockQueue(PreQueue):
    """Example of extending PreQueue, for documentation and testing purposes."""
    def __init__(self):
        PreQueue.__init__(self)
        self._items = []
        self._pending = 0

    def _store(self, item):
        """Store the given item in the queue.

        Return True if the item was actually stored (and so any
        blocked consumer threads should be woken up).

        When this method is invoked, the queue's Condition is acquired
        and the queue is known to be not closed.
        """
        self._items.append(item)
        return True

    def _itemavailable(self):
        """Whether an item is available now.

        When this method is invoked, the queue's Condition is acquired
        and the queue is known to be not closed.
        """
        return len(self._items) > 0

    def _yielditem(self):
        """Produce an item to be returned.

        When this method is invoked, the queue's Condition is acquired,
        the queue is known not to be closed, and _itemavailable()
        just returned True.
        """
        item = self._items[0]
        del self._items[0]
        self._pending += 1
        return item

    def _itemspending(self):
        """Whether items have been produced but not processed.

        It is up to the subclass to implement, in _yielditem(), any
        bookkeeping for keeping track of such items, and to implement,
        typically by a method called task_done(), some facility for
        the consumers of items to inform the queue that an item has
        been processed.

        When this method is invoked, the queue's Condition is acquired
        and the queue is known not to be closed.
        """
        return self._pending > 0

    def task_done(self):
        """Inform the queue that an item previously returned by get() has been processed."""
        with self._condition:
            self._pending -= 1
            self._condition.notifyAll()

class NeophiliacQueue(PreQueue):
    """A queue-like object which is concerned with only the most recent item.

    Items are numbered as they pass through the queue.

    >>> q = NeophiliacQueue()
    >>> q.put('a')
    >>> q.get()
    (1, 'a')
    >>> q.put('b')
    >>> q.get()
    (2, 'b')

    Each item added displaces any item that has not yet been removed.

    >>> q = NeophiliacQueue()
    >>> q.put('a')
    >>> q.put('b')
    >>> q.get()
    (1, 'b')

    Since only the most recent item is of interest, when an item
    has been processed, all lower-numbered items are considered
    processed.

    >>> q = NeophiliacQueue()
    >>> q.put('a')
    >>> t = testingthread(target=q.join)
    >>> q.get()
    (1, 'a')
    >>> q.put('b')
    >>> q.get()
    (2, 'b')
    >>> q.task_done(2)  # task 2 is finished, so task 1 doesn't matter
    >>> t.join(0.1); t.isAlive()
    False
    >>> q.task_done(1)  # has no effect
    """
    def __init__(self):
        PreQueue.__init__(self)
        self._newitem = False
        self._lastnum = 0
        self._maxpending = None
        self._maxdone = None

    def _store(self, item):
        self._item = item
        self._newitem = True
        return True

    def _itemavailable(self):
        return self._newitem

    def _yielditem(self):
        self._lastnum += 1
        item = self._item
        self._item = None
        self._newitem = False
        self._maxpending = self._lastnum
        return self._lastnum, item

    def _itemspending(self):
        return self._maxpending is not None \
            and (self._maxdone is None or self._maxdone < self._maxpending)

    def task_done(self, num):
        """Inform the queue that an item previously returned by get() has been processed.

        Informing a closed queue of a processed item is not an error,
        but has no effect.

        >>> q = NeophiliacQueue()
        >>> q.put('a')
        >>> q.get()
        (1, 'a')
        >>> q.close()
        >>> q.task_done(1)
        """
        with self._condition:
            if self._maxpending is None or num > self._maxpending:
                raise ValueError("task_done() called for invalid task %r" % num)
            self._maxdone = num
            self._condition.notifyAll()

class HighestItemQueue(PreQueue):
    """A queue-like object which retains only the highest item.

    >>> q = HighestItemQueue()
    >>> q.put('A')
    >>> q.put('C')
    >>> q.put('B')
    >>> q.get()
    'C'

    A comparison function (with the same behaviour as cmp()) can be passed
    at creation time.

    >>> q = HighestItemQueue(cmp=lambda x,y: -cmp(x,y))
    >>> q.put(1)
    >>> q.put(2)
    >>> q.get()
    1

    All items returned by get() must be processed before joined threads
    will unblock:

    >>> q = HighestItemQueue()
    >>> q.put('x')
    >>> t = testingthread(target=q.join)
    >>> t.join(0.1); t.isAlive()  # waiting for x to be extracted
    True
    >>> q.get()
    'x'
    >>> t.join(0.1); t.isAlive()  # waiting for x to be processed
    True
    >>> q.put('y')
    >>> q.get()
    'y'
    >>> q.task_done()             # one task is done
    >>> t.join(0.1); t.isAlive()  # waiting for the other task
    True
    >>> q.task_done()
    >>> t.join(0.1); t.isAlive()
    False
    """
    def __init__(self, cmp=cmp):
        PreQueue.__init__(self)
        self._cmp = cmp
        self._newitem = False
        self._pending = 0

    def _store(self, item):
        if self._newitem and self._cmp(self._item, item) >= 0:
            return False
        self._item = item
        self._newitem = True
        return True

    def _itemavailable(self):
        return self._newitem

    def _yielditem(self):
        item = self._item
        self._item = None
        self._newitem = False
        self._pending += 1
        return item

    def _itemspending(self):
        return self._pending > 0

    def task_done(self):
        """Inform the queue that an item previously returned by get() has been processed."""
        with self._condition:
            if self._pending <= 0:
                raise ValueError("task_done() called more times than get()")
            self._pending -= 1
            self._condition.notifyAll()

class Busy(object):
    def __init__(self, statusbar=None, message=None):
        self._statusbar = statusbar
        self._message = message
    def __enter__(self):
        wx.BeginBusyCursor()
        if self._statusbar is not None and self._message is not None:
            self._statusbar.PushStatusText(self._message)
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._statusbar is not None and self._message is not None:
            self._statusbar.PopStatusText()
        wx.EndBusyCursor()
        return False  # let exception propagate

class ErrorDialog(object):
    def __init__(self, message, caption):
        self._message = message
        self._caption = caption
    def __enter__(self):
        pass
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is None:
            return False
        else:
            wx.MessageBox("%s\nError message: %s" % (self._message, exc_val),
                self._caption, wx.OK | wx.ICON_ERROR)
            return True  # stop exception from propagating

class Searcher(object):
    def __init__(self, db, queryq, resultsq):
        self._db = db
        self._queryq = queryq
        self._resultsq = resultsq
        self._thread = threading.Thread(target=self._run)
    def _run(self):
        for (num, query) in queueiter(self._queryq):
            try:
                results = self._db.search(query)
            except ValueError:
                pass
            else:
                self._resultsq.put((num,
                    [(item['id'], item['title'], results.score(n))
                        for n, item in enumerate(results)]))
            self._queryq.task_done(num)
    def start(self):
        self._thread.start()
    def join(self):
        self._thread.join()

class EditBox(wx.TextCtrl):
    UnloadEvent, EVT_UNLOAD = newevent.NewEvent()
    LoadEvent, EVT_LOAD = newevent.NewEvent()
    SaveEvent, EVT_SAVE = newevent.NewEvent()
    DeleteEvent, EVT_DELETE = newevent.NewEvent()

    def __init__(self, parent, db):
        wx.TextCtrl.__init__(self, parent, style=wx.TE_MULTILINE)
        self._db = db
        self._id = None
        self._title = None
        self._savefailed = False
        self.Disable()

    def GetNoteTitle(self):
        return self._title
    def GetNoteID(self):
        return self._id
    def IsNoteLoaded(self):
        return self._id is not None
    def DidSaveFail(self):
        return self._savefailed

    def SetNote(self, id):
        if self._id is not None:
            wx.PostEvent(self, self.UnloadEvent(box=self, id=self._id))
            self._id = None
            self._title = None
            self.ChangeValue('')
            self.DiscardEdits()
        if id is None:
            self.Disable()
        else:
            try:
                text = self._db.read(id)
            except:
                self.Disable()
                raise
            self._id = id
            self._title = text.splitlines()[0]
            self.ChangeValue(text)
            self.SetInsertionPoint(self.GetLastPosition())
            self.DiscardEdits()
            self.Enable()
            wx.PostEvent(self,
                self.LoadEvent(box=self, id=self._id, title=self._title))

    def _checkfail(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            self._savefailed = True
            self.SetStyle(0, self.GetLastPosition(), wx.TextAttr(wx.RED))
            raise

    def SaveNote(self):
        if self._id is not None and self.IsModified():
            text = self.GetValue().rstrip()
            if text == '':
                self._checkfail(self._db.remove, self._id)
                wx.PostEvent(self, self.DeleteEvent(box=self, id=self._id))
                self._id = None
                self._title = None
                self.Disable()
            else:
                self._checkfail(self._db.write, text, self._id)
                self._title = text.splitlines()[0]
                wx.PostEvent(self,
                    self.SaveEvent(box=self, id=self._id, title=self._title))
            self.DiscardEdits()
            self._savefailed = False
            self.SetStyle(0, self.GetLastPosition(), self.GetDefaultStyle())

class NotesFrame(wx.Frame):
    def __init__(self, *args, **kwargs):
        self._db = kwargs['db']
        del kwargs['db']
        wx.Frame.__init__(self, *args, **kwargs)

        self._queryq = NeophiliacQueue()
        self._resultsq = HighestItemQueue(cmp=lambda x,y: cmp(x[0],y[0]))
        self._resultsq.put = callafter(self._resultsq.put,
            wx.CallAfter, self._FlushResultsQueue)
        self._searcher = Searcher(self._db, self._queryq, self._resultsq)

        self._closed = False

        self.CreateStatusBar()

        self.panel = wx.Panel(self)
        self.searchbox = wx.TextCtrl(self.panel, style=wx.PROCESS_ENTER)
        self.searchbox.Bind(wx.EVT_TEXT, self.OnSearchChange)
        self.searchbox.Bind(wx.EVT_TEXT_ENTER, self.OnSearchEnterKey)
        self.resultsbox = wx.ListBox(self.panel)
        self.resultsbox.Disable()
        self.resultsbox.Bind(wx.EVT_SET_FOCUS, self.OnResultsEnter)
        self.resultsbox.Bind(wx.EVT_LISTBOX, self.OnResultsSelect)
        self.editbox = EditBox(self.panel, self._db)
        self.editbox.Bind(wx.EVT_KILL_FOCUS, self.OnEditboxLeave)
        self.editbox.Bind(EditBox.EVT_LOAD, self.OnNoteLoad)
        self.editbox.Bind(EditBox.EVT_UNLOAD, self.OnNoteUnload)
        self.editbox.Bind(EditBox.EVT_SAVE, self.OnNoteSave)
        self.editbox.Bind(EditBox.EVT_DELETE, self.OnNoteDelete)
        self.Bind(wx.EVT_CLOSE, self.OnClose)

        sizerL = wx.BoxSizer(wx.VERTICAL)
        sizerL.Add(self.searchbox, 0, wx.EXPAND)
        sizerL.Add(self.resultsbox, 1, wx.EXPAND)
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        sizer.Add(sizerL, 1, wx.EXPAND)
        sizer.Add(self.editbox, 1, wx.EXPAND)
        self.panel.SetSizerAndFit(sizer)

        self._searcher.start()
        self.SetStatusText(self._db.dirname)

    class _QueueWrapper(object):
        def __init__(self, q, func):
            self._q = q
            self._func = func
        def put(self, item):
            self._q.put(item)
            wx.CallAfter(self._func)

    @property
    def closed(self):
        return self._closed

    def OnClose(self, event):
        if not self._savechanges():
            if event.CanVeto():
                event.Veto()
                return
        self._queryq.close()
        with Busy("Waiting for search threads to die..."):
            self._searcher.join()
        self._closed = True
        event.Skip()

    def OnNoteLoad(self, event):
        self.SetStatusText(self._db.id2filename(event.id))
        self.SetTitle('Notes: %s' % event.title)
        event.Skip()
    def OnNoteSave(self, event):
        self.SetTitle('Notes: %s' % event.title)
        event.Skip()
    def OnNoteUnload(self, event):
        self.SetStatusText('')
        self.SetTitle('Notes')
        event.Skip()
    def OnNoteDelete(self, event):
        self.SetStatusText('')
        self.SetTitle('Notes')
        event.Skip()

    @ignoreifclosed
    def OnSearchChange(self, event):
        self._queryq.put(self.searchbox.GetValue())

    @ignoreifclosed
    def OnSearchEnterKey(self, event):
        if self.searchbox.IsEmpty():
            return
        if self.editbox.IsModified():
            answer = wx.MessageBox("Creating a new note will lose your unsaved changes!\n"
                + "Do you want to discard your unsaved changes?",
                "Unsaved changes will be lost!",
                wx.YES_NO | wx.ICON_EXCLAMATION)
            if answer != wx.YES:
                return
        try:
            with Busy(self, "Creating note..."):
                text = self.searchbox.GetValue().rstrip() + os.linesep
                newid = self._db.write(text)
        except Exception, e:
            wx.MessageBox(
                "Could not create your new note!\nError message: %s" % str(e),
                "Failed!", wx.OK | wx.ICON_ERROR)
        else:
            self.editbox.SetNote(newid)
            self.editbox.SetFocus()

    @ignoreifclosed
    def OnResultsEnter(self, event):
        with Busy(self, "Waiting for searches to finish..."):
            self._queryq.join()  # wait for searches to be turned into results
            self._FlushResultsQueue()  # put results in resultsbox now
            # fixme The above waits for all searches to finish,
            # not just the most recent one.
        # fixme if nothing selected, select first entry

    @ignoreifclosed
    def OnResultsSelect(self, event):
        if self.editbox.IsModified():
            self.SetStatusText(
                "Won't load another note while changes have not been saved!")
            return
        pos = self.resultsbox.GetSelection()
        if pos == wx.NOT_FOUND:
            self.editbox.SetNote(None)
            return
        try:
            with Busy(self, "Loading note..."):
                self.editbox.SetNote(self._results[pos][0])
        except Exception, e:
            wx.MessageBox("Could not load note!\nError message: %s" % str(e),
                "Failed!", wx.OK | wx.ICON_ERROR)

    @ignoreifclosed
    def OnEditboxLeave(self, event):
        self._savechanges()

    def _savechanges(self):
        if self.editbox.IsModified():
            try:
                with Busy(self, "Saving note..."):
                    self.editbox.SaveNote()
                    # fixme results box should receive note deletion events
            except Exception, e:
                wx.MessageBox(
                    "Could not save note!\nError message: %s" % str(e),
                    "Failed!", wx.OK | wx.ICON_ERROR)
                return False
        return True

    def _FlushResultsQueue(self):
        for (num, results) in queueiter(self._resultsq, block=False):
            results.sort(lambda x,y: cmp(x[1].lower(), y[1].lower()))
            self._results = results
            # fixme try to preserve old selection
            self.resultsbox.Set([t for i,t,s in results])
            # fixme fire unselection event if appropriate
            if len(results) == 0:
                self.resultsbox.Disable()
            else:
                self.resultsbox.Enable()
            self._resultsq.task_done()

class NotesApp(wx.App):
    def __init__(self, db, *args, **kwargs):
        self._db = db
        wx.App.__init__(self, *args, **kwargs)
    def OnInit(self):
        self.frame = NotesFrame(None, title='Notes', db=self._db)
        self.SetTopWindow(self.frame)
        self.frame.Show()
        self.frame.searchbox.SetFocus()
        return True

if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        print >>sys.stderr, "usage: %s dirname" % sys.argv[0]
        sys.exit(2)
    with NotesDB.open(sys.argv[1]) as db:
        NotesApp(db).MainLoop()
