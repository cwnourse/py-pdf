# -*- coding: utf-8 -*-
"""
parses PDF as text file.
uncompress pdf with pdftk first: pdftk in.pdf output out.pdf uncompress. 
otherwise will not be plain text

parser implemented based on ISO 32000-2:2020(E) (PDF2.0)

@author: noursec
"""
import time
import zlib
# from collections import namedtuple

# Token = namedtuple('Token', ['type','value','pos'])
class Token:
    def __init__(self,token_type,data,pos):
        self.type = token_type
        self.data = data
        self.pos = pos
    def __repr__(self):
        return f'Token<{self.type}><{self.data}><{self.pos}>'
    def __str__(self):
        return f'Token<{self.type}>'
    def __eq__(self, o):
        return self.type == o.type
    def __contains__(self,o):
        return self.data.__contains__(o)
    
        
def readBytes(file, chunk=16384):
    # generator to iterate over raw bytes of input file
    # experiment with different methods for speedup eg just copy whole pdf into RAM at once
    with open(file,'rb') as f:
        data = f.read()
        return data
        # while fchunk := f.read(chunk):
        #     yield from fchunk

class ByteReader():
    def __init__(self, filename, chunkSize=4096):
        self.filename = filename
        self.f = open(self.filename,'rb')
        self.size = self.f.seek(0,2)
        self.pos = self.f.seek(0)     # = 0
        self.chunkSize = chunkSize
        self.chunk = b''       
        self.chunkLength = -1  # length of chunk. should be same as chunk size except for last chunk
        self.chunkBegin = -1  # byte offset of first byte in chunk.
        self.lastChunk = False  # flag that there are no more bytes to read in file
    def __del__(self):
        self.f.close()
        
    ########################################
    # custom iterator methods
    ########################################
    
    def __iter__(self):
        return self
    def __next__(self):
        try:
            b = self.chunk[self.pos-self.chunkBegin]
            self.pos += 1
            return b
        except(IndexError):    # if bounds of chunk are exceeded or chunk is not loaded
            self.__loadChunk() # will raise StopIteration if no more chunks to load
            return next(self)  
    def __loadChunk(self):
        self.chunk = self.f.read(self.chunkSize)      
        self.chunkBegin = self.pos
        # self.chunkLength = len(self.chunk)
        if not self.chunk:
            raise StopIteration
    def seek(self,pos):
        # this method should alter behavior of next method to return byte from file pointer position
        if pos > self.size:
            return False
        self.f.seek(pos)
        self.pos = pos
        self.chunk = self.f.read(self.chunkSize)
        self.chunkBegin = pos
        return True
    
    ###########################################
    # Generators 
    ###########################################
   
    def readAll(self,pos=0):  
        # highest-performance iterator over file, but most expensive memorywise
        with open(self.filename,'rb') as f:
            f.seek(pos)
            self.data = f.read()
            return iter(self.data)  # faster than yield from self.data
    def readChunks(self,chunkSize=2048,pos=0):
        # higher performance than custom iterator, but worse than readAll
        with open(self.filename,'rb') as f:
            f.seek(pos)
            while chunk:=f.read(chunkSize):
                yield from chunk
    def readReverse(self):
        # very slow, but not super critical... only used once
        with open(self.filename,'rb') as f:
            f.seek(-1,2)
            while f.tell()!=0:       
                b = f.read(1)    # reverse byte iteration not used much, so its ok to do the slow simple method here vs. yielding from chunks
                f.seek(-2,1)
                yield ord(b)
    

class PdfInterpreter():
    # char classes.
    CHAR_WS = {0,9,10,12,13,32}                        # + [b'\x00',b'\t',b'\n',b'\x0c',b'\r',b' ']                     
    CHAR_EOL = {10,13}                                   # + [b'\n',b'\r']
    CHAR_DELIM = {40,41,60,62,91,93,123,125,47,37}      # + [b'(',b')',b'<',b'>',b'[',b']',b'{',b'}',b'/',b'%']                           
    CHAR_INT = {48,49,50,51,52,53,54,55,56,57}           # + [b'0',b'1',b'2',b'3',b'4',b'5',b'6',b'7',b'8',b'9']
    CHAR_NUM = CHAR_INT | {43,45,46}                   # + [b'+',b'-',b'.']  
    CHAR_NONREG = CHAR_WS | CHAR_DELIM 
    # KEYWORDS = {'obj','endobj',b'stream',b'endstream','R','true','false','xref','f','n','trailer','startxref'}
    
    def __init__(self,filename):
        self.filename = filename
        # self.reader = iter(readBytes(filename))       # 5.44MB/s
        # self.reader = ByteReader(filename)            # 3.90MB/s
        # self.reader = ByteReader(filename).readAll()  # 5.46MB/s
        self.reader = ByteReader(filename).readChunks() # 5.05MB/s
        self.objects = {}  # object dictionary: {(objNum, genNum): [Dict,Stream], ...}
        self.tokens = []   # stack of tokens
        self.bytes = []   # stack of read bytes
        self.pos = -1      # current byte offset into file such that file[pos]=bytes[-1]. 0=first byte
        self.line = 1      # current line number, delim by /n, /r, or /r/n
        self.peek = 0      # current 'look ahead' in file. nextByte returns from byte stack
        self.xrefLocation = None
        self.xref = []
        self.trailer = {}
        self.catalog = {}
        self.EOF = False
        
    def seek(self,offset):
        # updates the file reader so that next(self.reader) bgeins at specified offset
        # self.reader.seek(offset)  # if self.reader=ByteReader()
        self.reader = ByteReader(self.filename).readChunks(pos=offset)
        self.pos = offset
        
    def getXrefLocation(self):
        # returns the byte offset of the main xref table for this document
            # seek to this position and get next object to get xref table.
        # file always ends with '...startxref\n{INT}\n%%EOF\n'. read backwards to get this int
        lastByte = ByteReader(self.filename).readReverse()     # generator yields bytes from end
        while (b:=next(lastByte)) not in self.CHAR_INT: continue
        xrefloc = [b]
        while (b:=next(lastByte)) in self.CHAR_INT:
            xrefloc.append(b)
        # del lastByte
        self.xrefLocation = int(bytes(xrefloc[::-1]))
        
        return self.xrefLocation
        
    #################################################
    # internal data structure methods
    #################################################
        
    def flushStack(self):
        # flushes stack, returns buffered bytes. 
        # does not return 'peeked' bytes
        stack=self.bytes[:len(self.bytes)-self.peek]
        self.bytes = self.bytes[len(self.bytes)-self.peek:]
        return stack
    
    def pushToken(self,token:Token):
        self.tokens.append(token)

    # @profile
    def popByte(self,n=1): 
        # remove and return last tokens added to stack self.byte, oldest first order if n>1.
        # return [self.bytes.pop() for _ in range(n)][::-1]  # equivalent and more pythonic, but slower.
        pop,self.bytes = self.bytes[-1*n:],self.bytes[:-1*n]
        self.peek -= n if self.peek else 0               # make sure to decrement peek if present
        if self.peek<0: self.peek=0
        return pop  # raw int value of byte
        # return self.bytes.pop()
    
    def searchDict(self,d,param):
        # !!! modify so that param contained on top level is nearly as efficient as direct dict lookup.
            # ... I think it already is, aside from the overhead of creating the stack and loop
            # idea: try direct lookup first; if fail, then go right into recursion
        # breadth-first seach of dictionary that may contain nested dicts,arrays,bytestrings,and ints.
        # dictionary cannot contain any strings.
        # testdict = {b'1':1, b'2':{b'21':[211,b'212',{b'2131':2131}],b'22':22}, b'3':3}  # searchDict() able to finds all dict keys incl b'2131'!
        stack=[[d,iter(d)]]
        while stack:
            curd = stack[0][0]       # dict (keys), array (int,bytestring,dict,array), or bytestring (ints)
            curiter = stack[0][1]
            for key in curiter:
                try:
                    v = curd.get(key)
                    if key == param:
                        return v
                    else:
                        try:
                            viter = iter(v)
                        except TypeError:  # v is int
                            continue
                except AttributeError:  # curd is not a dict (no .get()). could be interested if it's a list. test if iterable. not interested if string. THERE ARE NO STRINGS! ONLY BYTESTRINGS WHICH ITERATES OVER INTS< NOT ITERABLE
                    try:
                        v = key
                        viter = iter(v) # works for array,dict,bytestring keys
                    except:                    # key is int
                        continue
                # if we get here, we didn't find they key yet and v is an iterable. add to stack
                stack.append([v,viter])
    
            else:
                stack.pop(0)  # pop iter from front when done
        return None
    
    def getObjParam(self,obj_id,paramName):
        # finds the value of an object's parameter
        # returns none if object doesn't define this parameter
        # possibly search parent objects for this attribute??
        obj = self.getObject(obj_id)
        params = obj[0]
        return self.searchDict(params,paramName)  # None if not found
        
        
    
        
    ################################################
    # data structure builders
    ################################################
    def updateXref(self,xref):
        # xref of format {(obj_id):loc, ...}
        # if 'trailer' in xref:                                   # separate xref, trailer, locs
        #     if 'trailer' in self.xref:
        #         self.xref['trailer'] |= xref['trailer']
        #     else:
        #         self.xref['trailer'] = xref['trailer']
                
        # if 'xref_loc' in xref:
        #     if 'xref_loc' in self.xref:
        #         self.xref['xref_loc'].append(xref['xref_loc'])
        #     else:
        #         self.xref['xref_loc'] = xref['xref_loc']
                
        # self.xref |= {k:v for k,v in xref.items() if k not in ['trailer','ref_loc']}
        
        self.xref |= xref  # adds entries together, overwrites duplicates (should be none doe to gen numbers) 
        
        return self.xref
        
    def buildXref(self,xref_loc=None):
        if xref_loc is None:  # main xref table at end
            # get xref location
            xref_loc = self.getXrefLocation()
        # seek to location
        self.seek(xref_loc)
        xref = self.nextObject()
        if xref == 'xref':         # xref table is already parsed and added to self.xref dict
            return self.xref
        elif len(xref)==2:         # xref is an object
            xref_obj = self.objects[xref]
            
            
        # get next object
            # OK modify to handle standard xref tables
        # parse object
            # if object:
                # decode according to filter
                # build dictionary {obj: offset,...} from index, stream, and W
                # follow /Prev, /Root, /Info and build these structures
        pass
        
    ################################################
    # data processing methods
    ###############################################
            
    def flateDecodeData(self,data):
        return zlib.decompress(data)
    def unpredict(self,data,columns):
        out = []
        j=0
        for i,b in enumerate(data):
            if not i%(columns+1): # each row introduced with a byte dictating the method of predition used for this row. 0=none,2=up
                method = b
            elif method==2:  # 'up' method
                up = out[j-columns] if j>columns-1 else 0
                out.append((b+up)%256)
                j+=1
            else:
                raise NotImplementedError(f'predition method {method} not implemented')
        return out
    
    ##############################################
    # tokenizer
    ###############################################
    def tokenize(self):
        if not self.EOF:
            while self.nextToken(): continue
        return None
        
    def newToken(self,token_type,data,position) -> Token:
        t = Token(token_type,data,position)
        self.tokens.append(t)
        return t
    
    # @profile
    def nextToken(self):
        # parse non-recurive PDF syntax tokens (ie. will make a token for a comment, but the parser will have to handle nested dictionaries etc)
        # BUT will not handle 'look back' tokens. ie to make an object token we have to see 'obj' or 'R' and then pop the last two int tokens to make object token.
        # backtracking behavior would not make sense in the context of nextToken as a generator, since we'd see int,ws,int,ws,OBJ=[int,int,'obj|'r'],
            # and it would not be efficient to look forward every time we see an int. we'll make the object in the parser
        
        # PDF binaries are typically computer generated. so we will not do rigorous syntax checking,
        # assume the input doc conforms to PDF spec (eg. comments shall be terminated by newline)
  
        if self.EOF:
            return False
        
        b = self.nextByte()
        
        pos=self.pos-self.peek       # start position (byte offset) of this token
        token_type = 'NONE'
        data=None
        
        if b in self.CHAR_WS:  # consume all whitespace chars including spaces and newline as one (7.2.3)
            token_type = 'CHAR_EOL' if b in self.CHAR_EOL else 'CHAR_WS'
            while (b:=self.nextByte()) in self.CHAR_WS:
                if b in self.CHAR_EOL: token_type = 'CHAR_EOL'  # no diff between ' \n', '     \n', '\n'. all EOL tokens
                continue
            self.peek += 1
            self.flushStack()
            # data = None
            return self.nextToken()  # don't create whitespace token. whitespace and newline serve no semantic purpose, we can insert them when rebuilding the document according to rules.
            
        elif b in self.CHAR_NUM:
            token_type = 'NUM_INT'
            while (p:=self.nextByte()) in self.CHAR_NUM:
                if p == 46: # b'.'
                    if self.nextByte() in self.CHAR_NUM: # if decimal encountered followed by another numeric, then its a REAL (float). ex. '4.' does not need to be a float, it can be '4'
                        token_type = 'NUM_REAL'
                    else:                               # skip decimal seperator if is not followed by number since it will mess up the int evaluation (int(b'4.') does not work)
                        pop = self.popByte(2)           # pop '.{^d}'
                        self.bytes.append(pop[-1])      # return non-numeric ^d to stack
                        self.peek += 1
            self.peek += 1
            data = self.flushStack()
            data = int(bytes(data)) if token_type=='NUM_INT' else float(bytes(data))

        elif b in self.CHAR_DELIM:          
            if b==37:  # b'%':                           
                self.popByte()
                token_type = 'COMMENT'
                while self.nextByte() not in self.CHAR_EOL: continue  # read comment until end-of-line
                self.peek += 1
                data = bytes(self.flushStack())   # save comment text, because %PDF-1.x, %bbbb, and %%EOF will tokenize as comments and we should check for them in the builder. otherwise comment text could be ignored
            
            elif b==40:  # b'(':
                token_type = 'STR_LIT'
                self.popByte()                 # pop '(' delim
                n=1
                while n>0:                           # handle balanced unescaped parentheses in string
                    p = self.nextByte()     
                    if p==92:    # b'\\':            # '\' = 0x5c=92 escape character
                        self.nextByte()              # skip next byte, don't care if its a parenthesis cause it's escaped
                    elif p==40:  # b'(':             
                        n += 1
                    elif p==41:  # b')':
                        n -= 1     
                self.popByte()                 # pop ')' delim
                data = bytes(self.flushStack())
            
            elif b==47:  # b'/':
                self.popByte()
                token_type = 'NAME'           
                while self.nextByte() not in self.CHAR_NONREG: continue
                self.peek += 1
                data = bytes(self.flushStack())
            
            elif b==60:  # b'<':                     # hex string begin
                self.popByte()
                if self.nextByte()==60:  # b'<<':    # dict_begin '<<' token
                    token_type = 'DICT_BEGIN'
                    self.popByte()
                    # data = None
                else:
                    token_type = 'STR_HEX'
                    while not self.nextByte()==62:    # b'>':   # hex string end
                        continue
                    self.popByte()
                    data = bytes(self.flushStack())
                        
            elif b==62:  # b'>':
                self.popByte()
                if self.nextByte()==62:  # b'>':      # b'>>' dict_end token  
                    token_type = 'DICT_END'
                    self.popByte()
                    # data = None
                else:
                    self.peek += 1
                    print(f'error at pos {pos}: single > when >> expected')
                    return None
                
            elif b==91:  # b'[':
                self.popByte()
                token_type = 'ARR_BEGIN'
                # data=None
            elif b==93:  # b']':
                self.popByte()
                token_type = 'ARR_END'
                # data=None
                
            elif b==123:  # b'{':
                self.popByte()
                token_type = 'FN_BEGIN'
                # data=None
            elif b==125:  # b'}':
                self.popByte()
                token_type = 'FN_END'
                # data = None  
                        
            else:
                print(f'unhandled delim {b} at line {self.line}, byte {pos}')
                token_type = 'DELIM'
                data = self.flushStack() 
     
        else: # not whitespace, numeric, or delim. scan for regular chars
            while self.nextByte() not in self.CHAR_NONREG: continue
            
            self.peek += 1         
            keyword = bytes(self.flushStack())
            
            if keyword == b'R':
                token_type = 'OBJ_REF'
                # last two tokens are ints, if we ignore whitespace.. can get the last two tokens for object/generation numbers
                # objnum = self.tokens[-2]
                # gennum = self.tokens[-1]
                # data = (objnum.data, gennum.data)
                # pos = objnum.pos              
            elif keyword == b'n':
                token_type = 'XREF_INUSE'
                # data = None
            elif keyword == b'obj':
                token_type = 'OBJ_BEGIN'
                # last two tokens are ints, if we ignore whitespace.. can get the last two tokens for object/generation numbers
                # objnum = self.tokens[-2]
                # gennum = self.tokens[-1]
                # data = (objnum.data, gennum.data)
                # pos = objnum.pos
            elif keyword == b'endobj':
                token_type = 'OBJ_END'
                # data=None
            elif keyword == b'stream':
                token_type = 'STREAM'
                stream=True
                self.nextByte()  # 'stream' shall be followed by a single CRLF or LF, not CR. "The keyword stream that follows the stream dictionary shall be followed by an end-of-line marker consisting of either a CARRIAGE RETURN and a LINE FEED or just a LINE FEED, and not by a CARRIAGE RETURN alone."
                # print(f'{self.bytes=},{self.peek=}')
                self.popByte(len(self.bytes))  # either 1 or 2 bytes depending if we saw \r,\r\n
                # print(f'{self.bytes=},{self.peek=}')           
                while stream:  
                    if self.nextByte() in self.CHAR_EOL:                   
                        while self.nextByte() in self.CHAR_EOL: continue
                        if self.bytes[-1]==101:                    # 101 = ord(b'e')
                            if bytes(self.nextBytes(8)) == b'ndstream':  
                                [self.popByte() for _ in range(10)]  # pop '\nendstream' from stack
                                data = bytes(self.flushStack())
                                # data = (len(data),data)  # for checking consistency with /Length data preceeding string
                                stream = False
                            else:
                                self.peek += 8
            elif keyword == b'null':
                token_type = 'NULL'
                # data = None
            elif keyword == b'false':
                token_type = 'BOOL'
                data = False
            elif keyword == b'true':
                token_type = 'BOOL'
                data = True
            elif keyword == b'xref':
                token_type = 'XREF_BEGIN'
                # data = None
                # self.xrefLoc = pos
            elif keyword == b'f':
                token_type = 'XREF_FREE'
                # data = None
            elif keyword == b'trailer':
                token_type = 'TRAILER_BEGIN'
                # data = None
            elif keyword == b'startxref':
                token_type = 'XREF_LOC'
                # data = None    
            else:
                print(f'unhandled keyword {keyword}')
                token_type = 'REG'  # should never get here.
        return self.newToken(token_type, data, pos)               # appends token to class token list self.tokens. useful for debugging, not nedd for function
        # return Token(token_type,data,pos)                       # returns token, does NOT save to list
    
    # @profile    
    def nextByte(self):               
                                # code duplication. but, large speed increase from unrolling for loop in 
        # return getByte()
        if self.peek>0:                  # most common n=1 case and returning raw byte (int) rather than bytes object 
            b=self.bytes[-1*self.peek]   # ~2MB/s -> ~3MB/s
            self.peek -= 1
        else:
            try:                         # try/except is faster than checking for none value from iterator ('next(reader,None)')
                b=next(self.reader)
                self.bytes.append(b)
                self.pos += 1
                if b in self.CHAR_EOL:
                    self.line += 1
                    if b==13:     # 13=b'\r'. count /r{n} as 'n' lines. /r{n}/r/n counts as 'n'+1.   
                        while True:
                            bn=next(self.reader)
                            self.bytes.append(bn)
                            self.pos += 1
                            self.peek += 1
                            if bn==13:  # b'\r'
                                self.line += 1
                            else:
                                break
            except StopIteration:
                print('EOF exception') 
                self.EOF = True
                b = None           
        return b                    # returning raw byte b instead of bytes([b]) or bytes(bs) 
                                    # and converting to bytes where needed gives >4MB/s!!
    # @profile      
    def nextBytes(self,n):  # seperating nextBye(n=1) and nextByte(n=8) gives 10% speed boost 
        bs=[]
        for _ in range(n):          # this loop works for case n=1 also. 
            if self.peek>0:         # but there is overhead in creating a 1-iteration loop, so we create two nearly identical functions nextByte->Int and nextBytes->List[Int]
                bs.append(self.bytes[-1*self.peek])
                self.peek -= 1
            else:  
                try: 
                    b=next(self.reader)
                    bs.append(b)
                    self.bytes.append(b)
                    self.pos += 1
                    if b in self.CHAR_EOL:
                        self.line += 1
                        if b==13:     # 13=b'\r'. count /r{n} as 'n' lines. /r{n}/r/n counts as 'n'+1.   
                            while True:
                                bn=next(self.reader)
                                self.bytes.append(bn)
                                self.pos += 1
                                self.peek += 1
                                if bn==13:  # b'\r'
                                    self.line += 1
                                else:
                                    break
                except Exception as e:
                    print(f'EOF exception {e}') 
                    self.EOF = True
                    break
        return bs
    
    def newObject(self,objnum,gennum,data):
        key = (objnum,gennum)
        self.objects[key] = data
        return key
    
    def nextObject(self,stack=[]):
        # recursive function could stack overflow on eg. long strings of literals
        # mostly tail-call optimized, but not TCO in python anyway... 
        # consider changing to a while loop if stack overflow becomes a problem
        
        # if self.EOF:
        #     return False
        if not (token := self.nextToken()) or self.EOF:
            return False
        if token.type in ['NUM_REAL','NUM_INT','STR_LIT','STR_HEX','BOOL','NAME','STREAM','NULL']:
            stack.append(token.data)
            return self.nextObject(stack)  # stack overflow possible here if >1000 literals in a row...
        elif token.type in ['DICT_BEGIN','ARR_BEGIN']:
            stack.append(self.nextObject([]))
            return self.nextObject(stack)
        elif token.type == 'OBJ_REF':
            gennum,objnum = stack.pop(),stack.pop()
            stack.append({'REF':(objnum,gennum)})
            return self.nextObject(stack)
        elif token.type == 'OBJ_BEGIN':
            gennum,objnum = stack.pop(),stack.pop()
            objdata = self.nextObject([])
            return self.newObject(objnum,gennum,objdata)   # TOP LEVEL BASE CASE. RECURSION STOP
        elif token.type == 'DICT_END':
            return dict(zip(stack[::2],stack[1::2]))  # make key/value pairs of stack objects
        elif token.type in ['ARR_END','OBJ_END']:
            return stack
        elif token.type == 'COMMENT':
            # if comment text == b'%PDFx.y', b'%%EOF', do something...
            # for now just skip
            return self.nextObject(stack)
        elif token.type == 'XREF_BEGIN':  #'xref' keyword, start of xref table
            # build xref table here
            # stack = []  # assumed true.
            token_stack = []
            peek = 0
            obj_num = -1
            while True:
                for _ in range(3-peek):
                    token_stack.append(self.nextToken())
                    peek = 0
                if token_stack[2].type == 'XREF_INUSE':
                    token_stack.pop()              # index 2
                    obj_gen = token_stack.pop()    # index 1
                    obj_loc = token_stack.pop()    # index 0               
                    stack.append([(obj_num,obj_gen.data),obj_loc.data])
                    obj_num += 1
                    # token_stack = []
                elif token_stack[2].type == 'XREF_FREE':
                    token_stack.pop()
                    next_gen = token_stack.pop()
                    next_objnum = token_stack.pop()
                    stack.append([(obj_num,next_gen.data),{'FREE':next_objnum.data}])
                    obj_num += 1
                elif token_stack[2].type == 'NUM_INT':  # xref subsection header
                    obj_num = token_stack[0].data
                    # obj_cnt = token_stack[1]
                    token_stack = [token_stack[2]]
                    peek += 1
                elif token_stack[0].type == 'TRAILER_BEGIN':
                    # token_stack[0] == 'trailer_begin'
                    # token_stack[1] == 'dict_begin'
                    # token_stack[2] == first dict key
                    xref = dict(stack)
                    trailer_dict = self.nextObject([token_stack[2].data])
                    for _ in range(2):
                        token_stack.append(self.nextToken())
                    xref_loc = token_stack[-1].data
                    rv = (xref,trailer_dict,xref_loc)
                    self.xref.append(rv)
                    return (-1,0) # return 2-typle to keep types consistent. -1 flag corresponds to xref table, find it in self.xref[-1]
                elif token_stack[2].type == 'TRAILER_BEGIN':
                    # if subsection header has 0 objects. next token is 'dict_begin'
                    xref = dict(stack)
                    self.nextToken() # skip dict_begin so recursion terminates at dict_end
                    trailer_dict = self.nextObject()
                    for _ in range(2):
                        token_stack.append(self.nextToken())
                    xref_loc = token_stack[-1].data
                    rv = (xref,trailer_dict,xref_loc)
                    self.xref.append(rv)
                    return (-1,0) # return 2-typle to keep types consistent. -1 flag corresponds to xref table, find it in self.xref[-1]
                else:
                    print(f'unhandled xref tokens {token_stack}')
                    return False
            return False
        else:
            print(f'unhandled token {token}')
            print(f'{stack=}')
            return False
        
    def decompress(self,stream,filt,decodeparams):
        # filt either a bytestring or array of bytestrings
        # decode params either dict or array of dicts coresponding to filters
        if filt == b'FlateDecode':
            stm_dc = self.flateDecodeData(stream)                
            ## table 8: optional parameters for LZWDecode and FlateDecode filters
            if not decodeparams:
                predictor = 1
                colors = 1
                bitspercomponent = 8
                columns = 1
            else:
                predictor = self.searchDict(decodeparams, b'Predictor')
                colors = self.searchDict(decodeparams, b'Colors')
                bitspercomponent = self.searchDict(decodeparams, b'BitsPerComponent')
                columns = self.searchDict(decodeparams, b'Columns')
            # earlychange = self.searchDict(decodeparams, b'EarlyChange')  # LZW only  
            if predictor>1: stm_dc = self.unpredict(stm_dc,columns)  # !!! modify unpredict to take colors, bitspercomponent args
            return stm_dc
        else:
            raise NotImplementedError(f"decompress: filter type {filt} not implemented. TODO handle array of filters")
    
    # static method
    def hexBytesToInt(self,hexbytes):
        return sum([b*256**((len(hexbytes)-1)-i) for i,b in enumerate(hexbytes)])
        
    def parseXRefStm(self,xrefstm_id):
        # builds xref table contained in an object stream (7.5.8)
        # appends to xref with updated object table and trailer
        # ASSUMPTIONS: object must already be parsed from nextObject()
        xrs = self.objects[xrefstm_id]
        params = xrs[0]  # params should contain all entries in a stream (table 5), trailer (table 15), and xretstm
        stm = xrs[1]
        
        ## parse params according to spec tables:
        # table 5 entries common to all stream dictionaries:
        length = self.searchDict(params, b'Length')
        filt = self.searchDict(params, b'Filter')    # !!! returns either b'filtername' or [b'filter1',b'filter2',...]. only implemented for single filter at the moment
        decodeparams = self.searchDict(params,b'DecodeParams')
        f = self.searchDict(params,b'F')
        dl = self.searchDict(params, b'DL') or self.searchDict(params,b'Length1')  # decompressed length
        assert length == len(stm)
        if f: # if 'F' defined, stream data is contained in external file.     
            ffilt = self.searchDict(params,b'FFilter')
            fdecodeparams = self.searchDict(params,b'FDecodeParams')
            raise NotImplementedError(f'parseXRefStm: external file streams not yet supported')
        
        # table 15: entries in the file trailer dictionary
        size = self.searchDict(params, b'Size')
        prev = self.searchDict(params, b'Prev')
        root = self.searchDict(params, b'Root')
        encrypt = self.searchDict(params, b'Encrypt')
        info = self.searchDict(params, b'Info')
        pdf_id = self.searchDict(params, b'ID')
            
        # table 17: additional entries specific to a cross-reference stream dictionary
        obj_type = self.searchDict(params, b'Type')
        size = self.searchDict(params, b'Size')
        index = self.searchDict(params, b'Index')
        prev = self.searchDict(params, b'Prev')
        w = self.searchDict(params, b'W')
        assert obj_type==b'XRef', "parseXRefStm: XRef location does not point to a valid XRef object"  # this obj needs to be xref, this entry is required by spec
        if not index: index=[0,size]
        
        ## Decompress stream if filter present
        if filt:
            stm_dc = self.decompress(stm,filt,decodeparams)
            dl_meas = len(stm_dc)
        else:
            stm_dc = stm
            dl_meas = length
        if dl: assert dl_meas==dl
                    
        ## parse the xref stream according to W
        w_type = w[0]  # byte widths of xref stream fields
        w_obj = w[1]
        w_idx = w[2]
        w_entry = sum(w)
        
        assert (dl_meas % w_entry)==0  # ensure no entries gonna get cut off
        
        entries = [stm_dc[i:i+w_entry] for i in range(0,dl_meas,w_entry)]  # split stream by entry width (bytes)
        objnums = [n for objstart,nobj in zip(index[::2],index[1::2]) for n in range(objstart,objstart+nobj)]  # index=[69,2,420,3] -> objnums=[69,70,420,421,422]
        
        xref_update =[]        
        for objnum,entry in zip(objnums,entries):  # needs to iterate through chunks of stream equal to entry width w_entry
            etype = self.hexBytesToInt(entry[0:w_type]) if w_type!=0 else 1  # conflicting default information in table 18 (default 0) vs table 17 (default 1)
            eobj = self.hexBytesToInt(entry[w_type:w_type+w_obj]) if w_obj!=0 else 0 if etype==1 else None  # has a default value of zero (table 18, etype=1) but I'm not buying it
            eidx = self.hexBytesToInt(entry[w_type+w_obj:w_type+w_obj+w_idx])          
            if not eobj: 
                raise ValueError(f'parseXRefStm: bad field W={w}') # object field width should not be zero.          
            if etype==0:
                # free entry
                next_free_object = eobj       # object number of next free object
                gennum = eidx if eidx else 0   # generation number to be used if an object with this number is created again
                xref_entry = [(objnum,gennum),{'FREE':next_free_object}] 
            elif etype==1:
                # in-use object, not compressed
                byte_offset = eobj            # location of this object in bytes from beginning of file
                gennum = eidx if eidx else 0  # generation number of this objct (default:0)
                xref_entry = [(objnum,gennum),byte_offset]
            elif etype==2:
                # compressed object
                container_objnum = eobj  # The object number of the object stream in which this object is stored. (The generation number of the object stream shall be implicitly 0.)
                obj_index = eidx         # index within the compressed stream where this objct is stored               
                xref_entry = [(objnum,0),{'COMPRESSED':(container_objnum,obj_index)}]
            else:
                raise ValueError(f'xrefstm: bad entry type {etype}')
            xref_update.append(xref_entry)
        
        xref_update = dict(xref_update)
        trailer_update = {b'Size':size,
                         b'Prev':prev,
                         b'Root':root,
                         b'Encrypt':encrypt,
                         b'Info':info,
                         b'ID':pdf_id}
        loc_update = xrefstm_id
        rv = (xref_update,trailer_update,loc_update)
        self.xref.append(rv)
        for k,v in trailer_update.items():
            self.trailer[k] = v
        return (-1,0)    
        
        
            
            
            
            
            
        
            
    
    def getObjectFromXRef(self,obj_id):
        # obj_id := (objnum,gennum)
        # check in xref tables:
        for xref,trailer,loc in self.xref:
            if not (obj_loc := xref.get(obj_id)): continue
            self.seek(obj_loc)
            this_obj_id = self.nextObject()
            assert this_obj_id==obj_id
            return this_obj_id
        # if not found in current xref table, follow /XRefStm first, then /Prev pointers for the last xref
        # follow /XRefStm
        if (xrefstm_loc := self.searchDict(trailer,b'XRefStm')):
            self.seek(xrefstm_loc)
            xrefstm_id = self.nextObject()
            self.parseXRefStm(xrefstm_id)
        # check if in main xref table
            # if not, check /Prev
                # update main xref with this data and proceed
            # if not, check /XRefStm
                # update main xref with this data and proceed
            # if still not found, or free/deleted, return Null object
        # get byte offset of object
        # seek to position
        # obj = self.nextObject()
        # if object stream, parse the stream for the required object
            # parse all objs in stream to main object table
            pass

        
    
##############################################################


file = 'ISO_32000-2-2020_sponsored.pdf'
# file = 'engine_pyCopy.pdf'


interp = PdfInterpreter(file)

# i=0
# start=time.time()
# while interp.nextObject():
#     i+=1
#     pass
# end=time.time()
# print(f'read {i+1} objects in {end-start:0.1f}s, {(interp.pos+1)/(1024*1024)/(end-start):0.2f}MB/s')


# start=time.time()
# i=0
# while interp.nextToken():
#     # i+=1
#     # if not i%50000:
#     #     print(f'{time.time()-start:0.1f}s, pos={interp.pos}')
#     pass
# # interp.tokenize()
# end=time.time()
# print(f'read {interp.pos+1} bytes in {end-start:0.1f}s, {(interp.pos+1)/(1024*1024)/(end-start):0.2f}MB/s')

 


# TODO
# OK handle '<<' and '>>' delimiters OK
# OK handle hex string OK
# OK token parser:
    # OK convert <int> <int> <obj|R> to object token  (in parser)
        # handle 'endobj'
    # OK build nested arrays and dicts
    # OK Build xref, trailer!
# once parsed, write back to PDF file. successful if we can open as normal!
# gui for displaying hierarchy of doc objects
    # check boxes to include/exclude, then recompile PDF
# NO syntax error handling according to spec
# OK figure out whats so inefficient in this code!! it takes for fricken ever.
    # change from string appends to array appends sped up code like 1million times, now we are at 1.2mb/s from 0.04mb/s
    # beggest ineefficiency is the constant peeking, which is more expensive than reading. retool algorithm to avoid peaking
# OK handle ']' delim. doesnt work if name is last element etc [... /Name] makes a single name taken value='Name]' instead of two tokens 'Name' and ']'

         
# according to lineprofiler there don't appear to be any more bottle necks
# we are running at 2.63MB/s for tokenizing PDF data. this may be the fastest we can go
# over 90% of time taken by byte read operation so improvements here could help
# we havent yet tried just reading the whole file at once vs iterating through it bytewise in chunks, this might be quick
    # gives 2.87MB/s. saves 6 sec on 200mb file. I dont think this is gonna get faster..
    # last thing.... try some cython for shits.



# in the future, work on a rendering system
    # need to handle operators in this case
    
    
# THINGS I DID TO IMPROVE SPEED (<1MB/s -> 5MB/s):
    # dont append to strings
    # only convert type when neccesary
    # compare with integers
    # only read once (don't peek everything)
    # reading whole file vs lazy read
    # unroll for loop when 90% of them are only 1 loop
    # minimize compares
        # ie. dont check for EOF on every byte, just write it smarter so we dont have to
    
        # BEST SPEED SO FAR 6.46MB/s with parsing all objects :)
        
        
# OK BACK TO IT
    # make sure it can parse PDF spec docuent (linearized) OK
    # parse token stream
        # OK build object dictionary {id: {name1: {}, name2:{},...}, id2: ...}
        # OK build xref table/lookup dict
        # build bookmark list (catalog, pagetree)
        # get page number. Load all objects into memory via xref and create page object containing this data
    # generate PDF binary file from object structure.
        # make sure to generate proper byte offsets
    # once it can do this, remove erwin water marks and go back to wifi script
    
    
# where was I..... (today: 9/19/23)
    # decode xref stream and add it to the xref table!
    # then follow root to build pagetree and catalog
    # then build openPage() function to follow tree and load all page objects into memory
    # modify xref strategy to test for linearized pdf first

        